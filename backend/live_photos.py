"""Conservative Live Photo metadata pairing without reading image/video payloads.

Apple pairs the still's MakerApple tag 17 with the movie's QuickTime content
identifier. A timed still-image-time metadata track distinguishes live motion
from an ordinary movie. Filenames/duration alone never verify a pair.
The cache contains hashes of identifiers, not the identifiers themselves.
"""
from __future__ import annotations

import hashlib
import posixpath
import struct
import uuid
from collections import defaultdict
from backend.video_duration import mvhd_duration

VERSION = 1
MAX_METADATA = 1024 * 1024
PREFIX_SIZE = 64 * 1024
SUPPORTED_EXTENSIONS = {".mov", ".jpg", ".jpeg", ".heic", ".heif"}
CONTENT_KEY = b"com.apple.quicktime.content.identifier"
STILL_KEY = b"com.apple.quicktime.still-image-time"


class InvalidMetadata(ValueError):
    pass


def _u(data, offset, count, order="big"):
    if count not in {0, 1, 2, 4, 8} or offset < 0 or offset + count > len(data):
        raise InvalidMetadata()
    return int.from_bytes(data[offset:offset+count], order)


def _identifier(value):
    try:
        text = value.decode("utf-8").strip("\x00 \r\n")
        identifier = str(uuid.UUID(text))
    except (ValueError, UnicodeError, AttributeError):
        return None
    return hashlib.sha256(identifier.encode("ascii")).hexdigest()


def atoms(data, offset=0, end=None):
    end = len(data) if end is None else end
    count = 0
    while offset + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            size = _u(data, offset+8, 8)
            header = 16
        if size == 0:
            size = end-offset
        if size < header or offset+size > end or count >= 4096:
            raise InvalidMetadata()
        yield kind, data[offset+header:offset+size], offset+header
        offset += size
        count += 1
    if offset != end:
        raise InvalidMetadata()


def _children(data, kind):
    return [body for key, body, _ in atoms(data) if key == kind]


def _meta_identifier(meta):
    # ISO meta is a full box; Apple's QuickTime meta may omit that header.
    start = 0 if len(meta) >= 8 and meta[4:8] in {b"hdlr", b"keys", b"ilst"} else 4
    children = list(atoms(meta, start))
    keys = next((data for kind, data, _ in children if kind == b"keys"), None)
    ilst = next((data for kind, data, _ in children if kind == b"ilst"), None)
    if keys is None or ilst is None or len(keys) < 8:
        return None
    count, offset, key_index = _u(keys, 4, 4), 8, None
    if count > 4096:
        raise InvalidMetadata()
    for index in range(1, count+1):
        size = _u(keys, offset, 4)
        if size < 8 or offset+size > len(keys):
            raise InvalidMetadata()
        if keys[offset+4:offset+8] == b"mdta" and keys[offset+8:offset+size] == CONTENT_KEY:
            key_index = index
        offset += size
    if key_index is None:
        return None
    for kind, payload, _ in atoms(ilst):
        if int.from_bytes(kind, "big") != key_index:
            continue
        for subkind, value, _ in atoms(payload):
            if subkind == b"data" and len(value) >= 8 and _u(value, 0, 4) == 1:
                return _identifier(value[8:])
    return None


def _movie_metadata(moov):
    identifiers = []
    duration = None
    for kind, body, _ in atoms(moov):
        if kind == b"mvhd":
            duration = mvhd_duration(body)
        elif kind == b"meta":
            value = _meta_identifier(body)
            if value:
                identifiers.append(value)
        elif kind == b"udta":
            for meta in _children(body, b"meta"):
                value = _meta_identifier(meta)
                if value:
                    identifiers.append(value)
    timed = False
    for track in _children(moov, b"trak"):
        for media in _children(track, b"mdia"):
            handlers = _children(media, b"hdlr")
            if not any(len(body) >= 12 and body[8:12] == b"meta" for body in handlers):
                continue
            for minf in _children(media, b"minf"):
                for stbl in _children(minf, b"stbl"):
                    for stsd in _children(stbl, b"stsd"):
                        if STILL_KEY in stsd:
                            timed = True
    unique = set(identifiers)
    return {"version": VERSION, "identifierHash": next(iter(unique)) if len(unique) == 1 else None,
            "stillImageTime": timed, "durationSeconds": duration}


class _Reader:
    def __init__(self, read_at, size):
        self.read_at, self.size = read_at, size
        self.used = 0
        self.prefix = b""
    def read(self, offset, count):
        if offset < 0 or count < 0 or offset+count > self.size:
            raise InvalidMetadata()
        if offset+count <= len(self.prefix):
            return self.prefix[offset:offset+count]
        if self.used+count > MAX_METADATA:
            raise InvalidMetadata()
        self.used += count
        result = self.read_at(offset, count)
        if len(result) != count:
            raise InvalidMetadata()
        return bytes(result)
    def prefetch(self):
        self.prefix = self.read(0, min(PREFIX_SIZE, self.size))


def _movie(reader):
    offset = 0
    for _ in range(128):
        if offset+8 > reader.size:
            break
        header = reader.read(offset, 8)
        size, kind = struct.unpack(">I4s", header)
        length = 8
        if size == 1:
            size = _u(reader.read(offset+8, 8), 0, 8)
            length = 16
        if size == 0:
            size = reader.size-offset
        if size < length or offset+size > reader.size:
            raise InvalidMetadata()
        if kind == b"moov":
            return _movie_metadata(reader.read(offset+length, size-length))
        # Jump over mdat: even a multi-gigabyte movie needs only box headers.
        offset += size
    return {"version": VERSION, "identifierHash": None, "stillImageTime": False}


def _maker_identifier(note):
    if len(note) < 16 or note[:12] != b"Apple iOS\x00\x00\x01" or note[12:14] not in {b"II", b"MM"}:
        return None
    order = "little" if note[12:14] == b"II" else "big"
    count = _u(note, 14, 2, order)
    if count > 1024 or 16+count*12 > len(note):
        raise InvalidMetadata()
    for index in range(count):
        offset = 16+index*12
        tag, kind = _u(note, offset, 2, order), _u(note, offset+2, 2, order)
        length, value = _u(note, offset+4, 4, order), _u(note, offset+8, 4, order)
        if tag == 17 and kind == 2 and 36 <= length <= 64:
            if value+length > len(note):
                raise InvalidMetadata()
            return _identifier(note[value:value+length])
    return None


def _exif_identifier(tiff):
    if len(tiff) < 8 or tiff[:2] not in {b"II", b"MM"}:
        return None
    order = "little" if tiff[:2] == b"II" else "big"
    if _u(tiff, 2, 2, order) != 42:
        return None
    def entry(ifd, requested):
        count = _u(tiff, ifd, 2, order)
        if count > 4096 or ifd+2+count*12 > len(tiff):
            raise InvalidMetadata()
        for index in range(count):
            at = ifd+2+index*12
            if _u(tiff, at, 2, order) == requested:
                return _u(tiff, at+2, 2, order), _u(tiff, at+4, 4, order), _u(tiff, at+8, 4, order)
        return None
    pointer = entry(_u(tiff, 4, 4, order), 0x8769)
    if not pointer or pointer[:2] != (4, 1):
        return None
    maker = entry(pointer[2], 0x927c)
    if not maker or maker[0] != 7 or maker[1] > MAX_METADATA or maker[2]+maker[1] > len(tiff):
        return None
    return _maker_identifier(tiff[maker[2]:maker[2]+maker[1]])


def _jpeg(reader):
    if reader.read(0, 2) != b"\xff\xd8":
        return None
    offset = 2
    for _ in range(128):
        marker = reader.read(offset, 2)
        if marker[0] != 255 or marker[1] in {0xda, 0xd9}:
            return None
        length = _u(reader.read(offset+2, 2), 0, 2)
        if length < 2:
            raise InvalidMetadata()
        if marker[1] == 0xe1 and reader.read(offset+4, min(6, length-2)) == b"Exif\0\0":
            return _exif_identifier(reader.read(offset+10, length-8))
        offset += 2+length
    return None


def _heif(reader):
    offset = 0
    for _ in range(128):
        if offset+8 > reader.size:
            return None
        size, kind = struct.unpack(">I4s", reader.read(offset, 8))
        header = 8
        if size == 1:
            size, header = _u(reader.read(offset+8, 8), 0, 8), 16
        if size < header or offset+size > reader.size:
            raise InvalidMetadata()
        if kind == b"meta":
            meta_offset = offset+header
            meta = reader.read(meta_offset, size-header)
            break
        offset += size
    else:
        return None
    children = list(atoms(meta, 4))
    iinf = next((body for key, body, _ in children if key == b"iinf"), None)
    iloc = next((body for key, body, _ in children if key == b"iloc"), None)
    if not iinf or not iloc:
        return None
    exif_ids = []
    for key, info, _ in atoms(iinf, 6 if iinf[0] == 0 else 8):
        if key != b"infe" or info[0] not in {2, 3}:
            continue
        width = 2 if info[0] == 2 else 4
        if info[6+width:10+width] == b"Exif":
            exif_ids.append(_u(info, 4, width))
    if len(exif_ids) != 1 or iloc[0] not in {0, 1, 2}:
        return None
    version = iloc[0]
    offset_size, length_size = iloc[4] >> 4, iloc[4] & 15
    base_size, index_size = iloc[5] >> 4, iloc[5] & 15 if version in {1, 2} else 0
    width = 4 if version == 2 else 2
    count, at = _u(iloc, 6, width), 6+width
    if count > 4096:
        raise InvalidMetadata()
    for _ in range(count):
        item_id = _u(iloc, at, width); at += width
        construction = 0
        if version in {1, 2}:
            construction = _u(iloc, at, 2) & 15; at += 2
        reference = _u(iloc, at, 2); at += 2
        base = _u(iloc, at, base_size); at += base_size
        extents = _u(iloc, at, 2); at += 2
        if extents > 4096:
            raise InvalidMetadata()
        locations = []
        for _ in range(extents):
            at += index_size
            location = _u(iloc, at, offset_size); at += offset_size
            length = _u(iloc, at, length_size); at += length_size
            locations.append((base+location, length))
        if item_id != exif_ids[0] or reference != 0 or construction not in {0, 1} or len(locations) != 1:
            continue
        location, length = locations[0]
        if construction == 1:
            idat = next((start for key, _, start in children if key == b"idat"), None)
            if idat is None:
                return None
            location += meta_offset+idat
        data = reader.read(location, length)
        return _exif_identifier(data[4+_u(data, 0, 4):])
    return None


def inspect_metadata(read_at, size, name):
    """At most 1 MiB per file, with no dependency, decode or original saved."""
    reader = _Reader(read_at, size)
    result = {"version": VERSION, "identifierHash": None, "stillImageTime": False}
    try:
        extension = posixpath.splitext(name)[1].lower()
        if extension == ".mov":
            return _movie(reader)
        if extension not in {".jpg", ".jpeg", ".heic", ".heif"}:
            return result
        reader.prefetch()
        value = _jpeg(reader) if extension in {".jpg", ".jpeg"} else _heif(reader)
        result["identifierHash"] = value
    except (InvalidMetadata, IndexError, struct.error):
        pass
    return result


def group_items(items):
    """Verified 1:1 pairs only. Ambiguous and unpaired movies stay visible."""
    photos, videos = defaultdict(list), defaultdict(list)
    for item in items:
        metadata = item.get("_livePhotoMetadata", {})
        identifier = metadata.get("identifierHash")
        if item.get("source") != "photos" or metadata.get("version") != VERSION or not isinstance(identifier, str) or len(identifier) != 64:
            continue
        key = (item.get("deviceId"), item.get("parentPath", posixpath.dirname(item["path"])), identifier)
        if item.get("kind") == "photo":
            photos[key].append(item)
        elif item.get("kind") == "video" and metadata.get("stillImageTime") is True:
            videos[key].append(item)
    pairs, hidden = {}, set()
    for key, images in photos.items():
        motions = videos.get(key, [])
        if len(images) == len(motions) == 1:
            candidates = [(images[0], motions[0])]
        else:
            candidates = []
            for photo in images:
                stem = posixpath.splitext(photo["name"])[0].casefold()
                same_images = [item for item in images if posixpath.splitext(item["name"])[0].casefold() == stem]
                same_motions = [item for item in motions if posixpath.splitext(item["name"])[0].casefold() == stem]
                if len(same_images) == len(same_motions) == 1:
                    candidates.append((photo, same_motions[0]))
        for photo, motion in candidates:
            pairs[photo["id"]] = motion
            hidden.add(motion["id"])
    output = []
    for item in items:
        if item["id"] in hidden:
            continue
        value = dict(item)
        value.pop("livePhoto", None)
        if item["id"] in pairs:
            motion = dict(pairs[item["id"]])
            motion.pop("livePhoto", None)
            value["livePhoto"] = {"video": motion, "verified": True, "totalSize": item["size"]+motion["size"]}
        output.append(value)
    return output
