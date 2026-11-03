"""Range and capability tests use fake phone bytes, never a real iPhone."""
import io
import socket
import struct
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from backend.afc_io import _recv_exact, read as afc_read, iter_read
from backend.engine import UserError
from backend.streaming import byte_range
from backend import test_backend as _fixtures


class ByteRangeTests(unittest.TestCase):
    def test_bounded_single_ranges(self):
        self.assertEqual(byte_range(None, 10), (0, 9, False))
        self.assertEqual(byte_range("bytes=2-7", 10), (2, 7, True))
        self.assertEqual(byte_range("bytes=7-", 10), (7, 9, True))
        self.assertEqual(byte_range("bytes=-3", 10), (7, 9, True))
        self.assertEqual(byte_range("bytes=7-500", 10), (7, 9, True))
        for value in ("bytes=3-2", "bytes=10-", "bytes=-0", "bytes=0-1,4-5", "bytes=-", "invalid"):
            with self.assertRaises(ValueError):
                byte_range(value, 10)

    def test_direct_receive_fragmentation_and_disconnect(self):
        class Fragmented:
            def __init__(self, data):
                self.stream = io.BytesIO(data)
            def recv_into(self, buffer):
                return self.stream.readinto(buffer[:3])
        self.assertEqual(_recv_exact(Fragmented(b"fragmented packets"), 18), b"fragmented packets")
        with self.assertRaises(ConnectionAbortedError):
            _recv_exact(Fragmented(b"short"), 6)

    def test_direct_afc_read_rejects_malformed_and_oversized_framing(self):
        class Socket:
            def __init__(self, data):
                self.stream = io.BytesIO(data)
            def recv_into(self, buffer):
                return self.stream.readinto(buffer)
        class Service:
            pass
        class AFC:
            def __init__(self, data):
                self.service = Service()
                self.service.socket = Socket(data)
            def _dispatch_packet(self, operation, data):
                pass
        valid = struct.pack("<8sQQQQ", b"CFA6LPAA", 45, 40, 0, 2) + b"hello"
        self.assertEqual(afc_read(AFC(valid), 1, 5), b"hello")
        for data in (struct.pack("<8sQQQQ", b"notmagic", 45, 40, 0, 2),
                     struct.pack("<8sQQQQ", b"CFA6LPAA", 200, 40, 0, 2),
                     struct.pack("<8sQQQQ", b"CFA6LPAA", 45, 40, 0, 18) + b"hello"):
            with self.assertRaises(ConnectionAbortedError):
                afc_read(AFC(data), 1, 5)

    def native_afc(self, chunks):
        class Socket:
            def __init__(self, contents):
                self.contents = io.BytesIO(contents)
            def recv_into(self, buffer):
                return self.contents.readinto(buffer)
        class AFC:
            def __init__(self):
                self.service = type("Service", (), {})()
                contents = b"".join(struct.pack("<8sQQQQ", b"CFA6LPAA", 40 + len(chunk), 40, index, 2) + chunk for index, chunk in enumerate(chunks))
                self.service.socket = Socket(contents)
                self.requests = []
                self.closed = 0
            def _dispatch_packet(self, operation, data):
                self.requests.append(struct.unpack("<QQ", data)[1])
            def close(self):
                self.closed += 1
        return AFC()

    def test_pipelined_reads_are_ordered_and_close_pending_requests_on_pause(self):
        afc = self.native_afc([b"abcd", b"efgh", b"ij"])
        chunks = iter_read(afc, 1, 10, chunk_size=4)
        self.assertEqual(next(chunks), b"abcd")
        self.assertEqual(afc.requests, [4, 4])
        self.assertEqual(b"".join(chunks), b"efghij")
        self.assertEqual(afc.requests, [4, 4, 2])
        self.assertEqual(afc.closed, 0)
        afc = self.native_afc([b"abcd", b"efgh", b"ij"])
        chunks = iter_read(afc, 1, 10, chunk_size=4)
        self.assertEqual(next(chunks), b"abcd")
        chunks.close()
        self.assertEqual(afc.closed, 1)

    def test_short_native_reply_does_not_skip_or_commit_missing_bytes(self):
        afc = self.native_afc([b"ab", b"efgh"])
        with self.assertRaises(EOFError):
            list(iter_read(afc, 1, 8, chunk_size=4))
        self.assertEqual(afc.closed, 1)


# Reuse only the fixture, without inheriting all existing test methods.
class StreamTests(unittest.TestCase):
    setUp = _fixtures.EngineTests.setUp
    tearDown = _fixtures.EngineTests.tearDown
    download = _fixtures.EngineTests.download

    def open(self, url, method="GET", headers=None):
        return urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers or {}), timeout=3)

    def test_phone_range_head_revocation_and_authoritative_cached_item(self):
        content = bytes(range(256)) * 32
        job = self.download(content)
        supplied = {**job["item"], "path": "/Documents/private.txt", "size": 1}
        result = self.engine.handle("stream", {"item": supplied, "preferLocal": False})
        self.assertFalse(result["downloaded"])
        self.assertEqual(result["size"], len(content))
        self.assertFalse(self.afc.read_calls)
        with self.open(result["url"], "HEAD") as response:
            self.assertEqual(response.headers["Content-Length"], str(len(content)))
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertFalse(self.afc.read_calls)
        with self.open(result["url"], headers={"Range": "bytes=100-199"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], f"bytes 100-199/{len(content)}")
            self.assertEqual(response.read(), content[100:200])
        self.assertEqual(self.afc.seek_calls, [100])
        self.assertEqual(self.afc.read_paths, [job["item"]["path"]])
        self.assertFalse(self.engine.handle("stop_stream", {"url": "http://example.com/evil/original"})["stopped"])
        self.assertTrue(self.engine.handle("stop_stream", {"url": result["url"]})["stopped"])
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.open(result["url"])
        self.assertEqual(failure.exception.code, 404)
        self.assertFalse(self.afc.handles)

    def test_range_validation_host_restriction_and_changed_source(self):
        job = self.download()
        result = self.engine.stream(job["item"], prefer_local=False)
        for headers, status in (({"Range": "bytes=0-1,5-6"}, 416), ({"Range": "bytes=999-"}, 416), ({"Host": "example.com"}, 403)):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.open(result["url"], headers=headers)
            self.assertEqual(failure.exception.code, status)
        self.assertFalse(self.afc.read_calls)
        self.afc.files[job["item"]["path"]].extend(b"changed")
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.open(result["url"])
        self.assertEqual(failure.exception.code, 409)
        self.assertFalse(self.afc.read_calls)

    def test_local_stream_works_offline_and_detects_file_change(self):
        content = b"already downloaded original"
        job = self.download(content)
        self.engine.run_job(job)
        self.engine._devices[self.device_id]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Local playback should never open USB")):
            result = self.engine.stream(job["item"])
            self.assertTrue(result["downloaded"])
            self.assertEqual(result["source"], "pc")
            with self.open(result["url"], headers={"Range": "bytes=-8"}) as response:
                self.assertEqual(response.read(), content[-8:])
            Path(job["localPath"]).write_bytes(b"replaced file")
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.open(result["url"])
            self.assertEqual(failure.exception.code, 409)

    def test_stream_file_regular_documents_and_offline_error(self):
        path = "/Documents/note.txt"
        self.afc.add(path, b"ordinary document")
        item = self.engine._catalogue_item(self.device_id, "app", "test.documents", path, self.afc.stat(path))
        self.engine._catalogues["app-document"] = [item]
        with self.assertRaises(UserError):
            self.engine.stream(item)
        result = self.engine.handle("stream_file", {"item": item})
        with self.open(result["url"]) as response:
            self.assertEqual(response.read(), b"ordinary document")
        token = result["url"].split("/")[3]
        self.assertGreater(self.engine._media.sessions[token]["deadline"] - time.monotonic(), 11 * 3600)
        self.engine._devices[self.device_id]["online"] = False
        with self.assertRaises(UserError):
            self.engine.stream(item, any_file=True)

    def test_native_drag_preparation_uses_cached_metadata_and_defers_phone_access(self):
        job = self.download(b"cached authoritative file")
        supplied = {**job["item"], "size": 1, "modified": "forged", "path": "/Documents/private.txt"}
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Preparing a drag must not open the phone")):
            result = self.engine.handle("stream_file", {"item": supplied, "preferLocal": False})
            self.assertEqual(result["size"], len(b"cached authoritative file"))
            self.assertEqual(result["modified"], job["item"]["modified"])
            with self.open(result["url"], "HEAD") as response:
                self.assertEqual(response.headers["Content-Length"], str(result["size"]))
        self.assertFalse(self.afc.read_calls)
        with self.open(result["url"]) as response:
            self.assertEqual(response.read(), b"cached authoritative file")
        self.assertEqual(self.afc.read_paths, [job["item"]["path"]])

    def test_native_drag_defers_real_source_changes_and_symlink_checks_until_get(self):
        job = self.download(b"catalogued before the drag")
        result = self.engine.handle("stream_file", {"item": job["item"], "preferLocal": False})
        self.afc.files[job["item"]["path"]].extend(b"changed")
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.open(result["url"])
        self.assertEqual(failure.exception.code, 409)
        self.assertFalse(self.afc.read_calls)
        self.afc.links.add("/DCIM/100APPLE")
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.open(result["url"])
        self.assertEqual(failure.exception.code, 503)
        self.assertFalse(self.afc.read_calls)

    def test_empty_document_native_stream_has_zero_length(self):
        path = "/Documents/empty.txt"
        self.afc.add(path, b"")
        item = self.engine._catalogue_item(self.device_id, "app", "test.documents", path, self.afc.stat(path))
        self.engine._catalogues["empty-document"] = [item]
        result = self.engine.handle("stream_file", {"item": item})
        with self.open(result["url"]) as response:
            self.assertEqual(response.headers["Content-Length"], "0")
            self.assertEqual(response.read(), b"")

    def test_session_limit_preserves_active_transfer_resources(self):
        job = self.download()
        result = self.engine.stream(job["item"])
        server = self.engine._media
        token = result["url"].split("/")[3]
        session = server.sessions[token]
        class Active:
            def shutdown(self, how):
                pass
            def close(self):
                pass
        resource = Active()
        server.resource(session, resource)
        with mock.patch("backend.streaming.MAX_SESSIONS", 1):
            with self.assertRaises(UserError):
                self.engine.stream(job["item"])
            self.assertIn(token, server.sessions)
            self.assertFalse(session["cancel"].is_set())
            server.resource(session, resource, False)
            self.engine.stream(job["item"])
            self.assertNotIn(token, server.sessions)

    def test_engine_close_kills_attached_preview_decoder(self):
        job = self.download()
        result = self.engine.stream(job["item"])
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.engine._media.attach_process(result["url"], process)
            self.engine.close()
            process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def test_thumbnail_read_budget_stops_before_full_original(self):
        job = self.download(b"a" * (3 * 1024 * 1024))
        server = self.engine._media_server()
        result = server.register(job["item"], budget=1024 * 1024)
        with self.open(result["url"]) as response:
            with self.assertRaises(Exception):
                response.read()
        self.assertEqual(sum(count for _, count in self.afc.read_calls), 1024 * 1024)
        self.assertFalse(self.afc.handles)

    def test_app_photo_and_photo_without_native_thumbnail_preview_automatically(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (1200, 600), "red").save(encoded, "JPEG")
        self.afc.add("/Documents/image.jpg", encoded.getvalue())
        app = self.engine._catalogue_item(self.device_id, "app", "test.documents", "/Documents/image.jpg", self.afc.stat("/Documents/image.jpg"))
        self.engine._catalogues["app-photo"] = [app]
        result = self.engine.thumbnail(app)
        self.assertTrue(result["available"])
        self.assertTrue(Path(result["path"]).is_file())
        photo = self.download(encoded.getvalue(), "without-ios-thumb.jpg")
        self.assertTrue(self.engine.thumbnail(photo["item"])["available"])


if __name__ == "__main__":
    unittest.main()
