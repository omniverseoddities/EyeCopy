"""Cached previews remain available after restart without contacting a device."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.engine import Engine, PREVIEW_LIMIT, device_key
from backend.test_backend import FakeAFC


class CachedThumbnailTests(unittest.TestCase):
    def setUp(self):
        output = Path(__file__).resolve().parent.parent / "test-output"
        output.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="cached-thumbnail-", dir=output)
        self.root = Path(self.temporary.name)
        self.afc = FakeAFC()
        self.phone = device_key("cached-preview-fixture")

        @contextlib.contextmanager
        def connector(*_args):
            yield self.afc

        self.engine = Engine(self.root / "data", download_dir=self.root / "downloads", connector=connector, start=False)
        self.engine._devices[self.phone] = {"id": self.phone, "name": "Fixture", "online": False,
                                            "defaultDownloadDir": str(self.root / "downloads" / "Fixture")}
        self.engine.selected_device = self.phone

    def tearDown(self):
        self.engine.close()
        self.temporary.cleanup()

    def item(self, source="photos", kind="photo", size=100):
        suffix = "jpg" if kind == "photo" else "mov"
        path = f"/DCIM/100APPLE/fixture.{suffix}" if source == "photos" else f"/Documents/fixture.{suffix}"
        item = self.engine._catalogue_item(self.phone, source, "test.documents" if source == "app" else None,
                                          path, {"st_ifmt": "S_IFREG", "st_size": size, "st_mtime": self.afc.modified})
        scope = self.engine.scope(self.phone, source, item.get("appId"))
        self.engine._catalogues[scope] = [item]
        self.engine.current_scope = scope
        return item

    def cache(self, item, native=False):
        from PIL import Image
        identifier = hashlib.sha256(json.dumps([item["id"], item["modified"], item["size"]]).encode()).hexdigest()[:32]
        folder = self.engine.cache_dir / self.phone
        folder.mkdir(exist_ok=True)
        target = folder / (("thumb-" if native else "") + identifier + ".jpg")
        Image.new("RGB", (48, 32), "orange").save(target, "JPEG")
        return target

    def test_photo_preview_reused_after_offline_restart(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (1200, 600), "orange").save(encoded, "JPEG")
        item = self.item(size=len(encoded.getvalue()))
        self.afc.add(item["path"], encoded.getvalue())
        preview = self.engine.preview(item)
        self.assertTrue(self.afc.read_paths)
        self.assertFalse(Path(preview["path"]).name.startswith("thumb-"))
        self.engine.save()
        self.engine.close()
        self.engine = Engine(self.root / "data", start=False)
        self.assertFalse(self.engine._devices[self.phone]["online"])
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Cached preview must not open USB")) as usb, \
             mock.patch.object(self.engine, "_app_thumbnail", side_effect=AssertionError("Cached preview needs no decoder")):
            result = self.engine.thumbnail(item)
            usb.assert_not_called()
        self.assertTrue(result["available"])
        self.assertEqual(result["path"], preview["path"])

    def test_cached_oversize_app_photo_bypasses_automatic_limit_and_busy_decoder(self):
        item = self.item(source="app", size=PREVIEW_LIMIT + 1)
        target = self.cache(item)
        with mock.patch.object(self.engine, "_app_thumbnail", side_effect=AssertionError("Cache comes before limits")), \
             mock.patch.object(self.engine, "_afc", side_effect=AssertionError("No phone read")) as usb:
            self.assertEqual(self.engine.thumbnail(item)["path"], str(target))
            usb.assert_not_called()

    def test_cached_app_video_does_not_require_ffmpeg(self):
        item = self.item(source="app", kind="video")
        target = self.cache(item, native=True)
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(self.engine, "_app_thumbnail", side_effect=AssertionError("No video decoding")), \
             mock.patch.object(self.engine, "_afc", side_effect=AssertionError("No phone read")) as usb:
            self.assertEqual(self.engine.thumbnail(item)["path"], str(target))
            usb.assert_not_called()

    def test_native_thumbnail_is_preferred_when_both_cache_variants_exist(self):
        item = self.item()
        self.cache(item)
        target = self.cache(item, native=True)
        self.assertEqual(self.engine.thumbnail(item)["path"], str(target))

    def test_online_cached_photo_also_skips_device_access(self):
        item = self.item()
        target = self.cache(item)
        self.engine._devices[self.phone]["online"] = True
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Existing cache comes first")) as usb:
            self.assertEqual(self.engine.thumbnail(item)["path"], str(target))
            usb.assert_not_called()

    def test_changed_size_or_modified_time_never_reuses_old_preview(self):
        for field, changed in (("size", 101), ("modified", "2026-10-30T00:00:00+00:00")):
            with self.subTest(field=field):
                item = self.item()
                self.cache(item)
                item[field] = changed
                with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Offline cache miss is optional")) as usb:
                    self.assertFalse(self.engine.thumbnail(item)["available"])
                    usb.assert_not_called()

    def test_supplied_stale_metadata_does_not_override_authoritative_signature(self):
        item = self.item()
        self.cache(item)
        stale = dict(item)
        item["size"] += 1
        self.assertFalse(self.engine.thumbnail(stale)["available"])

    def test_cache_does_not_bypass_allowed_device_paths(self):
        item = self.item()
        item["path"] = "/Documents/private.jpg"
        self.cache(item)
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Invalid path must not open USB")) as usb:
            self.assertFalse(self.engine.thumbnail(item)["available"])
            usb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
