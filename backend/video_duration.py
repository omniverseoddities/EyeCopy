"""Bounded MOV/MP4 duration metadata; no decoder and no media payload reads.

QuickTime's movie-header time scale and duration describe the entire movie:
https://developer.apple.com/documentation/quicktime-file-format/movie_header_atom
ISO BMFF version 1 expands creation/modification time and duration to 64 bits.
"""
import math
import posixpath
import struct

VERSION = 1
MAX_READ_BYTES = 64 * 1024
MAX_TOP_BOXES = 128
MAX_MOVIE_CHILDREN = 4096
SUPPORTED_EXTENSIONS = {".mov", ".mp4", ".m4v"}


class InvalidDuration(ValueError):
    pass


def mvhd_duration(body: bytes) -> float | None:
    if not body or body[0] not in {0, 1}:
        return None
    version = body[0]
    scale_at, duration_at, width = (12, 16, 4) if version == 0 else (20, 24, 8)
    if len(body) < duration_at + width:
        return None
    scale = int.from_bytes(body[scale_at:scale_at + 4], "big")
    duration = int.from_bytes(body[duration_at:duration_at + width], "big")
    if scale == 0 or duration == (1 << (width * 8)) - 1:
        return None
    value = duration / scale
    return value if math.isfinite(value) and value >= 0 else None


class Reader:
    def __init__(self, read_at, size):
        self.read_at, self.size, self.used = read_at, size, 0

    def read(self, offset, count):
        if offset < 0 or count < 0 or offset + count > self.size or self.used + count > MAX_READ_BYTES:
            raise InvalidDuration()
        self.used += count
        value = self.read_at(offset, count)
        if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) != count:
            raise InvalidDuration()
        return bytes(value)

    def boxes(self, start, end, limit):
        offset = start
        for _ in range(limit):
            if offset == end:
                return
            if offset + 8 > end:
                raise InvalidDuration()
            size, kind = struct.unpack(">I4s", self.read(offset, 8))
            header = 8
            if size == 1:
                if offset + 16 > end:
                    raise InvalidDuration()
                size = int.from_bytes(self.read(offset + 8, 8), "big")
                header = 16
            if size == 0:
                size = end - offset
            if size < header or offset + size > end:
                raise InvalidDuration()
            yield kind, offset + header, offset + size
            offset += size
        if offset != end:
            raise InvalidDuration()


def inspect_duration(read_at, size: int, name: str) -> float | None:
    """Seek over media and large moov children; read at most 64 KiB total."""
    if (not isinstance(size, int) or isinstance(size, bool) or size < 8
            or posixpath.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS):
        return None
    reader = Reader(read_at, size)
    try:
        for kind, start, end in reader.boxes(0, size, MAX_TOP_BOXES):
            if kind != b"moov":
                continue
            for child, body_at, body_end in reader.boxes(start, end, MAX_MOVIE_CHILDREN):
                if child == b"mvhd":
                    return mvhd_duration(reader.read(body_at, min(32, body_end - body_at)))
    except (InvalidDuration, struct.error, OverflowError, IndexError):
        pass
    return None
