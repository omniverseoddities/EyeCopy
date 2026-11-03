"""Codec-independent duration metadata from bounded synthetic movie headers."""
import copy
import struct
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from backend.engine import SourceChangedError
from backend.video_duration import MAX_READ_BYTES, inspect_duration, mvhd_duration
from backend import test_backend as fixtures
from backend import test_live_photos as live_fixtures


def box(kind, payload=b"", extended=False):
    return (struct.pack(">I4sQ", 1, kind, len(payload) + 16) if extended
            else struct.pack(">I4s", len(payload) + 8, kind)) + payload


def header(ticks=125375, scale=1000, version=0):
    if version == 0:
        return bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, scale, ticks)
    return bytes([version, 0, 0, 0]) + struct.pack(">QQIQ", 0, 0, scale, ticks)


def movie(ticks=125375, scale=1000, version=0, tail=False):
    moov = box(b"moov", box(b"mvhd", header(ticks, scale, version)) + box(b"trak", b"unsupported-codec-data"))
    media = box(b"mdat", b"media-bytes-that-must-not-be-read" * 10)
    return box(b"ftyp", b"qt  ") + (media + moov if tail else moov + media)


class DurationParserTests(unittest.TestCase):
    def inspect(self, value, name="video.MOV"):
        reads = []
        result = inspect_duration(lambda at, count: (reads.append((at, count)), value[at:at+count])[1], len(value), name)
        self.assertLessEqual(sum(count for _, count in reads), MAX_READ_BYTES)
        return result, reads

    def test_movie_header_v0_and_v1_fractional_durations_head_and_tail(self):
        for version in (0, 1):
            for tail in (False, True):
                ticks = 125375 if version == 0 else 2**32 + 17
                value = movie(ticks=ticks, version=version, tail=tail)
                for name in ("video.MOV", "video.mp4", "video.m4v"):
                    with self.subTest(version=version, tail=tail, name=name):
                        duration, reads = self.inspect(value, name)
                        self.assertAlmostEqual(duration, ticks / 1000)
                        self.assertLess(sum(count for _, count in reads), 100)

    def test_zero_valid_unknown_flags_versions_and_bad_timescales(self):
        self.assertEqual(mvhd_duration(header(0)), 0)
        self.assertIsNone(mvhd_duration(header(scale=0)))
        self.assertIsNone(mvhd_duration(header(ticks=2**32-1)))
        self.assertIsNone(mvhd_duration(header(ticks=2**64-1, version=1)))
        self.assertIsNone(mvhd_duration(header(version=2)))
        for count in range(20):
            self.assertIsNone(mvhd_duration(header()[:count]))

    def test_multi_gigabyte_media_and_large_movie_track_boxes_are_skipped(self):
        prefix = box(b"ftyp", b"qt  ")
        media_size = 10 * 1024**3
        track_size = 100 * 1024**2
        moov_at = len(prefix) + media_size
        mvhd_at = moov_at + 8 + track_size
        mvhd = box(b"mvhd", header())
        moov_size = 8 + track_size + len(mvhd)
        blocks = {0: prefix, len(prefix): struct.pack(">I4sQ", 1, b"mdat", media_size),
                  moov_at: struct.pack(">I4s", moov_size, b"moov"),
                  moov_at + 8: struct.pack(">I4sQ", 1, b"trak", track_size), mvhd_at: mvhd}
        reads = []
        def read(at, count):
            reads.append((at, count))
            for start, value in blocks.items():
                if start <= at and at + count <= start + len(value):
                    return value[at-start:at-start+count]
            self.fail("Duration inspection attempted a media or track payload read.")
        self.assertEqual(inspect_duration(read, moov_at + moov_size, "large.mov"), 125.375)
        self.assertLess(sum(count for _, count in reads), 100)

    def test_extended_zero_length_and_malformed_boxes_remain_bounded(self):
        valid = box(b"moov", box(b"mvhd", header(), extended=True), extended=True)
        self.assertEqual(self.inspect(valid)[0], 125.375)
        size_zero = struct.pack(">I4s", 0, b"moov") + box(b"mvhd", header())
        self.assertEqual(self.inspect(size_zero)[0], 125.375)
        for value in (b"bad", struct.pack(">I4s", 7, b"moov"), struct.pack(">I4s", 99999, b"moov"),
                      struct.pack(">I4s", 1, b"moov") + b"short", box(b"moov", box(b"mvhd", b"short")),
                      box(b"moov", box(b"free") * 4100 + box(b"mvhd", header()))):
            with self.subTest(length=len(value)):
                self.assertIsNone(self.inspect(value)[0])
        self.assertEqual(self.inspect(movie(), "unsupported.avi"), (None, []))


class DurationEngineTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    tearDown = fixtures.EngineTests.tearDown

    def item(self, source="app", name="editing.mov", data=None):
        value = movie(tail=True) if data is None else data
        path = ("/Documents/" if source == "app" else "/DCIM/100APPLE/") + name
        self.afc.add(path, value)
        app = "test.documents" if source == "app" else None
        item = self.engine._catalogue_item(self.device_id, source, app, path, self.afc.stat(path))
        self.engine.current_scope = self.engine.scope(self.device_id, source, app)
        self.engine._catalogues[self.engine.current_scope] = [item]
        return item

    def test_files_app_scan_gets_duration_before_playback_and_reuses_signature_cache(self):
        item = self.item()
        self.engine._scan_generation = 1
        self.engine._scan(1, self.device_id, "app", "test.documents", "/Documents")
        self.assertEqual(self.engine.state()["catalogue"][0]["durationSeconds"], 125.375)
        self.assertLess(sum(count for _, count in self.afc.read_calls), 100)
        before = len(self.afc.read_calls)
        self.engine._scan_generation = 2
        self.engine._scan(2, self.device_id, "app", "test.documents", "/Documents")
        self.assertEqual(len(self.afc.read_calls), before)
        self.assertEqual(self.engine._jobs, [])
        self.assertFalse(Path(self.engine._devices[self.device_id]["defaultDownloadDir"]).exists())
        self.assertFalse(self.afc.handles)

    def test_lazy_remote_duration_needs_no_original_download_or_decoder_and_works_from_offline_cache(self):
        item = self.item("photos", data=movie(version=1))
        with mock.patch.object(self.engine, "_media_server", side_effect=AssertionError("No playback")):
            result = self.engine.handle("video_metadata", {"item": item})
        self.assertTrue(result["available"])
        self.assertEqual(result["durationSeconds"], 125.375)
        expected_signature = {"size": item["size"], "modified": item["modified"]}
        self.assertEqual(result["sourceSignature"], expected_signature)
        self.assertEqual(self.events[-1][0], "video_metadata")
        self.assertEqual(self.events[-1][1]["sourceSignature"], expected_signature)
        self.engine._devices[self.device_id]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Cached duration needs no USB")):
            cached_result = self.engine.video_metadata(item)
            self.assertEqual(cached_result["durationSeconds"], 125.375)
            self.assertEqual(cached_result["sourceSignature"], expected_signature)
        self.assertEqual(self.engine._jobs, [])
        self.assertFalse(self.afc.handles)

    def test_unknown_duration_is_negatively_cached_and_unsupported_formats_do_not_open_usb(self):
        item = self.item(data=box(b"moov", box(b"free")))
        self.assertFalse(self.engine.video_metadata(item)["available"])
        before = len(self.afc.read_calls)
        self.assertFalse(self.engine.video_metadata(item)["available"])
        self.assertEqual(len(self.afc.read_calls), before)
        item = self.item(name="editing.avi")
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Unsupported container")):
            self.assertTrue(self.engine.video_metadata(item)["unsupported"])

    def test_source_change_invalidates_cached_duration_and_change_during_probe_is_not_cached(self):
        item = self.item()
        old_result = self.engine.video_metadata(item)
        self.assertEqual(old_result["durationSeconds"], 125.375)
        self.afc.files[item["path"]] = bytearray(movie(ticks=250000))
        self.afc.modified += timedelta(seconds=1)
        self.engine._scan_generation = 1
        self.engine._scan(1, self.device_id, "app", "test.documents", "/Documents")
        self.assertEqual(self.engine.state()["catalogue"][0]["durationSeconds"], 250.0)
        new_result = self.engine.video_metadata(self.engine.state()["catalogue"][0])
        self.assertNotEqual(old_result["sourceSignature"], new_result["sourceSignature"])
        item = self.item(name="changing.mov")
        self.afc.after_read = lambda *args: setattr(self.afc, "modified", self.afc.modified + timedelta(seconds=1))
        with self.assertRaises(SourceChangedError):
            self.engine.video_metadata(item)
        self.assertNotIn("_videoMetadata", self.engine._catalogues[self.engine.current_scope][0])
        self.assertFalse(self.afc.handles)

    def test_offline_default_local_original_metadata_is_read_without_usb(self):
        item = self.item()
        self.engine.enqueue_download(self.device_id, [item], self.engine._devices[self.device_id]["defaultDownloadDir"])
        self.engine.run_job(self.engine._jobs[-1])
        self.engine._devices[self.device_id]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Use the verified default local original")):
            self.assertEqual(self.engine.video_metadata(item)["durationSeconds"], 125.375)

    def test_live_motion_duration_is_preserved_in_the_grouped_photo_and_lazy_result(self):
        still = live_fixtures.jpeg()
        original_motion = live_fixtures.movie()
        moov_at = original_motion.find(b"moov")
        motion = box(b"moov", box(b"mvhd", header(ticks=3000)) + original_motion[moov_at+4:])
        self.afc.add("/DCIM/100APPLE/LIVE.JPG", still)
        self.afc.add("/DCIM/100APPLE/LIVE.MOV", motion)
        self.engine.current_scope = self.engine.scope(self.device_id, "photos")
        self.engine._scan_generation = 1
        self.engine._scan(1, self.device_id, "photos", None, "/DCIM")
        photo = self.engine.state()["catalogue"][0]
        self.assertTrue(photo["livePhoto"]["verified"])
        self.assertEqual(photo["livePhoto"]["video"]["durationSeconds"], 3.0)
        self.assertEqual(self.engine.video_metadata(photo)["durationSeconds"], 3.0)


if __name__ == "__main__":
    unittest.main()
