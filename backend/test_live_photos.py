"""Synthetic Live metadata, false positives, cache and transfer semantics."""
import copy
import hashlib
import json
import io
import struct
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest import mock

from backend.engine import Engine, UserError
from backend.live_photos import inspect_metadata, group_items, MAX_METADATA
from backend import test_backend as fixtures

IDENTIFIER = "cf000001-1000-4000-a000-000000000001"
OTHER_ID = "cf000002-1000-4000-a000-000000000002"


def atom(kind, body):
    return struct.pack(">I4s", len(body)+8, kind)+body


def exif(identifier=IDENTIFIER):
    note = b"Apple iOS\0\0\1MM" + struct.pack(">H", 1)
    note += struct.pack(">HHII", 17, 2, 37, 32)+b"\0"*4+identifier.encode()+b"\0"
    root = struct.pack(">H", 1)+struct.pack(">HHII", 0x8769, 4, 1, 26)+b"\0"*4
    info = struct.pack(">H", 1)+struct.pack(">HHII", 0x927c, 7, len(note), 44)+b"\0"*4
    return b"MM\0*\0\0\0\x08"+root+info+note


def jpeg(identifier=IDENTIFIER):
    data = b"Exif\0\0"+exif(identifier)
    return b"\xff\xd8\xff\xe1"+struct.pack(">H", len(data)+2)+data+b"\xff\xda"+b"image-data"*10000


def movie(identifier=IDENTIFIER, timed=True, fullbox=False, payload=2048):
    key = b"com.apple.quicktime.content.identifier"
    keys = b"\0"*4+struct.pack(">I", 1)+struct.pack(">I", len(key)+8)+b"mdta"+key
    ilst = atom(struct.pack(">I", 1), atom(b"data", struct.pack(">II", 1, 0)+identifier.encode()))
    meta = atom(b"hdlr", b"\0"*8+b"mdta"+b"\0"*12)+atom(b"keys", keys)+atom(b"ilst", ilst)
    if fullbox:
        meta = b"\0"*4+meta
    track = b""
    if timed:
        track = atom(b"trak", atom(b"mdia", atom(b"hdlr", b"\0"*8+b"meta"+b"\0"*12)+atom(b"minf", atom(b"stbl", atom(b"stsd", b"\0"*8+b"com.apple.quicktime.still-image-time")))))
    return atom(b"ftyp", b"qt  \0\0\0\0")+atom(b"mdat", b"x"*payload)+atom(b"moov", atom(b"meta", meta)+track)


def heic(identifier=IDENTIFIER, construction=0):
    resource = b"\0"*4+exif(identifier)
    infe = atom(b"infe", b"\2\0\0\0"+struct.pack(">HH", 1, 0)+b"Exif"+b"Exif\0")
    iinf = atom(b"iinf", b"\0"*4+struct.pack(">H", 1)+infe)
    def iloc(location):
        return atom(b"iloc", b"\1\0\0\0\x44\0"+struct.pack(">HHHHHII", 1, 1, construction, 0, 1, location, len(resource)))
    ftyp = atom(b"ftyp", b"heic\0\0\0\0")
    if construction:
        return ftyp+atom(b"meta", b"\0"*4+iinf+iloc(0)+atom(b"idat", resource))
    meta = atom(b"meta", b"\0"*4+iinf+iloc(0))
    meta = atom(b"meta", b"\0"*4+iinf+iloc(len(ftyp)+len(meta)+8))
    return ftyp+meta+atom(b"mdat", resource+b"image-bytes"*10000)


class LiveMetadataTests(unittest.TestCase):
    def inspect(self, data, name):
        reads = []
        def read(offset, count):
            reads.append((offset, count))
            return bytearray(data[offset:offset+count])
        return inspect_metadata(read, len(data), name), reads

    def test_actual_maker_and_quicktime_layouts_match_without_payload_read(self):
        photo, reads = self.inspect(jpeg(), "IMG_1.JPG")
        motion, mov_reads = self.inspect(movie(payload=3*1024*1024), "IMG_1.MOV")
        self.assertTrue(photo["identifierHash"])
        self.assertEqual(photo["identifierHash"], motion["identifierHash"])
        self.assertTrue(motion["stillImageTime"])
        self.assertEqual(sum(count for _, count in reads), 65536)
        self.assertLess(sum(count for _, count in mov_reads), 1024)
        self.assertNotIn(IDENTIFIER, json.dumps(photo))
        fullbox, _ = self.inspect(movie(fullbox=True), "video.mov")
        self.assertEqual(fullbox["identifierHash"], motion["identifierHash"])

    def test_heif_exif_item_locations_absolute_and_in_idat(self):
        expected = hashlib.sha256(IDENTIFIER.encode()).hexdigest()
        for construction in (0, 1):
            data, reads = self.inspect(heic(construction=construction), "IMG_1.HEIC")
            self.assertEqual(data["identifierHash"], expected)
            self.assertLessEqual(sum(count for _, count in reads), 65536)

    def test_same_name_short_video_and_uuid_in_payload_are_not_verified_live(self):
        ordinary = atom(b"ftyp", b"qt  \0\0\0\0")+atom(b"mdat", IDENTIFIER.encode()+b"com.apple.quicktime.content.identifier com.apple.quicktime.still-image-time")+atom(b"moov", atom(b"mvhd", b"\0"*100))
        parsed, _ = self.inspect(ordinary, "IMG_1.MOV")
        self.assertFalse(parsed["identifierHash"])
        self.assertFalse(parsed["stillImageTime"])
        without_track, _ = self.inspect(movie(timed=False), "IMG_1.MOV")
        self.assertTrue(without_track["identifierHash"])
        self.assertFalse(without_track["stillImageTime"])
        misleading_photo = b"\xff\xd8\xff\xdaApple iOS\0\0\1MM"+IDENTIFIER.encode()
        self.assertFalse(self.inspect(misleading_photo, "IMG_1.JPG")[0]["identifierHash"])

    def test_oversized_malformed_and_large_box_offsets_remain_bounded(self):
        for data, name in ((b"not-image", "bad.jpg"), (struct.pack(">I4s", 2**32-1, b"moov"), "bad.mov"), (atom(b"moov", b"x"*(MAX_METADATA+1)), "bad.mov")):
            metadata, reads = self.inspect(data, name)
            self.assertFalse(metadata["identifierHash"])
            self.assertLessEqual(sum(count for _, count in reads), MAX_METADATA)
        # A >4GiB mdat is skipped, rather than allocating or reading its payload.
        offset = 5*1024**3
        prefix = atom(b"ftyp", b"qt  \0\0\0\0")
        moov = movie()[movie().find(b"moov")-4:]
        blocks = {0: prefix[:8], len(prefix): struct.pack(">I4s", 1, b"mdat"), len(prefix)+8: struct.pack(">Q", offset), len(prefix)+offset: moov[:8], len(prefix)+offset+8: moov[8:]}
        metadata = inspect_metadata(lambda at, count: blocks[at][:count], len(prefix)+offset+len(moov), "huge.mov")
        self.assertTrue(metadata["identifierHash"])
        self.assertTrue(metadata["stillImageTime"])


class LiveCatalogueTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    tearDown = fixtures.EngineTests.tearDown

    def pair(self, identifier=IDENTIFIER, timed=True, valid_image=False):
        photo_bytes, motion_bytes = jpeg(), movie(identifier, timed)
        if valid_image:
            from PIL import Image
            output = io.BytesIO()
            Image.new("RGB", (10, 10), "red").save(output, format="JPEG", exif=b"Exif\0\0"+exif())
            photo_bytes = output.getvalue()
        self.afc.files["/DCIM/IMG_1.JPG"] = bytearray(photo_bytes)
        self.afc.files["/DCIM/IMG_1.MOV"] = bytearray(motion_bytes)
        scope = self.engine.scope(self.device_id, "photos")
        self.engine.current_scope = scope
        self.engine._scan_generation = 1
        self.engine._scan(1, self.device_id, "photos", None, "/DCIM")
        return self.engine._catalogues[scope], photo_bytes, motion_bytes

    def test_scan_groups_verified_pair_and_keeps_metadata_cache_without_reread(self):
        items, photo, motion = self.pair()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["kind"], "photo")
        self.assertTrue(item["livePhoto"]["verified"])
        self.assertEqual(item["livePhoto"]["totalSize"], len(photo)+len(motion))
        self.assertEqual(item["livePhoto"]["video"]["kind"], "video")
        reads = len(self.afc.read_calls)
        self.engine._scan_generation = 2
        self.engine._scan(2, self.device_id, "photos", None, "/DCIM")
        self.assertEqual(len(self.afc.read_calls), reads)
        self.assertEqual(len(self.engine._catalogues[self.engine.current_scope]), 1)

    def test_false_positive_identifier_mismatch_missing_track_and_unpaired_stay_visible(self):
        for identifier, timed in ((OTHER_ID, True), (IDENTIFIER, False)):
            items, _, _ = self.pair(identifier, timed)
            self.assertEqual(len(items), 2)
            self.assertFalse(any(item.get("livePhoto") for item in items))
            self.engine._catalogues.clear()
        items, _, _ = self.pair()
        motion = copy.deepcopy(items[0]["livePhoto"]["video"])
        self.assertEqual(group_items([motion]), [motion])
        # Multiple same-ID images with no matching stems remain ambiguous.
        photo = copy.deepcopy(items[0]); photo.pop("livePhoto")
        duplicate = {**photo, "id": "different-id", "name": "different.JPG"}
        photo["name"] = "another.JPG"
        grouped = group_items([photo, duplicate, motion])
        self.assertEqual(len(grouped), 3)
        self.assertFalse(any(item.get("livePhoto") for item in grouped))

    def test_authoritative_expansion_deduplicates_and_ignores_forged_nested_video(self):
        items, _, _ = self.pair()
        photo = items[0]
        motion = photo["livePhoto"]["video"]
        forged = {**photo, "livePhoto": {"verified": True, "video": {"id": "forged", "path": "/Documents/private.txt"}}}
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Preparing a drag must not read the phone")):
            result = self.engine.handle("resolve_transfer_items", {"deviceId": self.device_id, "items": [forged, motion, photo]})
        self.assertEqual([item["id"] for item in result["items"]], [photo["id"], motion["id"]])
        self.assertFalse(any(item.get("livePhoto") for item in result["items"]))
        with self.assertRaises(UserError):
            self.engine.resolve_transfer_items([{ "id": "forged" }])
        with self.assertRaises(UserError):
            self.engine.resolve_transfer_items([photo], "different-phone")

    def test_group_downloads_both_components_and_nested_local_paths_survive_offline_cache(self):
        items, photo_bytes, motion_bytes = self.pair(valid_image=True)
        result = self.engine.enqueue_download(self.device_id, [items[0]], self.engine._devices[self.device_id]["defaultDownloadDir"])
        self.assertEqual(len(result["jobIds"]), 2)
        for job in self.engine._jobs:
            self.engine.run_job(job)
            self.assertEqual(job["state"], "completed")
        photo = self.engine._catalogues[self.engine.current_scope][0]
        self.assertEqual(Path(photo["localPath"]).read_bytes(), photo_bytes)
        self.assertEqual(Path(photo["livePhoto"]["video"]["localPath"]).read_bytes(), motion_bytes)
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False)
        try:
            restored._devices[self.device_id]["online"] = False
            cached = restored.state()["catalogue"]
            self.assertEqual(len(cached), 1)
            with mock.patch.object(restored, "_afc", side_effect=AssertionError("Offline cache must not read phone")):
                expanded = restored.resolve_transfer_items([cached[0]])["items"]
                preview = restored.preview(cached[0])
            self.assertTrue(all(Path(item["localPath"]).is_file() for item in expanded))
            self.assertTrue(Path(preview["path"]).is_file())
        finally:
            restored.close()

    def test_destination_only_live_queue_expands_cached_originals_without_phone_io(self):
        items, photo_bytes, motion_bytes = self.pair()
        photo = items[0]
        motion = photo["livePhoto"]["video"]
        destination = self.engine.data_dir / "live destination"
        self.engine._devices[self.device_id]["online"] = False
        before = len(self.afc.read_calls)
        forged = {**photo, "livePhoto": {"verified": True, "video": {"id": "forged", "path": "/Documents/private.txt"}}}
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Choosing a Live destination must only read cached metadata")):
            result = self.engine.handle("queue_download", {"deviceId": self.device_id, "items": [forged, motion, photo], "destination": str(destination)})
            self.assertEqual(len(result["jobIds"]), 2)
            self.assertEqual([job["item"]["id"] for job in self.engine._jobs], [photo["id"], motion["id"]])
            self.assertEqual([job["total"] for job in self.engine._jobs], [len(photo_bytes), len(motion_bytes)])
            self.assertTrue(all(job["awaitingStart"] and job["state"] == "paused" and job["bytes"] == 0 for job in self.engine._jobs))
            self.assertTrue(all(job["destination"] == str(destination.resolve()) for job in self.engine._jobs))
            self.assertIsNone(self.engine._next_work())
        self.assertEqual(len(self.afc.read_calls), before)
        self.assertFalse(destination.exists())

    def test_deleted_live_motion_copy_is_pruned_from_nested_cache_without_phone_access(self):
        items, _, _ = self.pair()
        photo = items[0]
        self.engine.enqueue_download(self.device_id, [photo], self.engine._devices[self.device_id]["defaultDownloadDir"])
        for job in self.engine._jobs:
            self.engine.run_job(job)
            self.assertEqual(job["state"], "completed")
        motion = photo["livePhoto"]["video"]
        deleted = motion["localPath"]
        Path(deleted).unlink()
        self.engine._devices[self.device_id]["online"] = False
        self.events.clear()
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Live drag resolution must not read phone")):
            result = self.engine.resolve_transfer_items([photo])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["offlineDeviceIds"], [self.device_id])
        self.assertTrue(Path(result["items"][0]["localPath"]).is_file())
        self.assertNotIn("localPath", result["items"][1])
        self.assertNotIn("localPath", photo["livePhoto"]["video"])
        self.assertFalse(self.events)
        saved = json.loads(self.engine.state_file.read_text())
        self.assertNotIn("localPath", saved["catalogues"][self.engine.current_scope][0]["livePhoto"]["video"])

    def test_changed_component_invalidates_cached_pairing(self):
        items, _, _ = self.pair()
        self.afc.files["/DCIM/IMG_1.MOV"] = bytearray(movie(OTHER_ID))
        self.afc.modified += timedelta(seconds=1)
        self.engine._scan_generation = 2
        self.engine._scan(2, self.device_id, "photos", None, "/DCIM")
        items = self.engine._catalogues[self.engine.current_scope]
        self.assertEqual(len(items), 2)
        self.assertFalse(any(item.get("livePhoto") for item in items))

    def test_old_offline_catalogue_hint_is_read_only_and_ignores_unsupported_files(self):
        scope = self.engine.scope(self.device_id, "photos")
        supported = {"id": "legacy", "name": "IMG_1.MOV", "kind": "video", "path": "/DCIM/IMG_1.MOV", "source": "photos", "deviceId": self.device_id}
        self.engine._catalogues[scope] = [supported]
        self.engine._devices[self.device_id]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Cache hint must not read phone")), mock.patch.object(self.engine, "request_scan", side_effect=AssertionError("Cache RPC only supplies a hint")):
            result = self.engine.handle("get_cached_catalogue", {"deviceId": self.device_id, "source": "photos"})
            self.assertTrue(result["needsLivePhotoScan"])
            self.assertEqual(result["items"], [supported])
            self.engine._catalogues[scope] = [{**supported, "name": "screen.PNG", "kind": "photo"}, {**supported, "name": "folder.JPG", "kind": "folder"}]
            self.assertFalse(self.engine.handle("get_cached_catalogue", {"source": "photos"})["needsLivePhotoScan"])
            app_scope = self.engine.scope(self.device_id, "app", "test.documents")
            self.engine._catalogues[app_scope] = [supported]
            self.assertFalse(self.engine.handle("get_cached_catalogue", {"source": "app", "appId": "test.documents"})["needsLivePhotoScan"])

    def test_current_negative_metadata_suppresses_hint_but_stale_nested_motion_requires_scan(self):
        items, _, _ = self.pair()
        self.assertFalse(self.engine.handle("get_cached_catalogue", {"source": "photos"})["needsLivePhotoScan"])
        photo = items[0]
        motion = photo["livePhoto"]["video"]
        for metadata in (None, {"version": 0}, {"version": True}):
            motion["_livePhotoMetadata"] = metadata
            self.assertTrue(self.engine.handle("get_cached_catalogue", {"source": "photos"})["needsLivePhotoScan"])
        motion["_livePhotoMetadata"] = {"version": 1, "identifierHash": None, "stillImageTime": False}
        self.assertFalse(self.engine.handle("get_cached_catalogue", {"source": "photos"})["needsLivePhotoScan"])


if __name__ == "__main__":
    unittest.main()
