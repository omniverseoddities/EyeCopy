"""Mock COM callbacks to verify Windows recycling never becomes permanent deletion."""
import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

from backend.pc_recycle import recycle_file


@unittest.skipUnless(os.name == "nt", "Windows COM contract")
class RecycleTests(unittest.TestCase):
    def run_fake(self, flags=0x80, validate=lambda: True, aborted=False, destination=True, hresult=0):
        import pythoncom
        from win32com.shell import shell
        path = Path(__file__).resolve().parents[1] / "test-output" / ("absent-com-fixture-" + uuid.uuid4().hex + ".mov")
        operation = mock.Mock()
        wrapped_sink = None

        def delete(item, sink):
            nonlocal wrapped_sink
            wrapped_sink = sink

        def perform():
            wrapped_sink.PreDeleteItem(flags, object())
            created = mock.Mock()
            created.GetDisplayName.return_value = "C:\\$Recycle.Bin\\S-1-5-21-1000\\$Rfixture.txt"
            wrapped_sink.PostDeleteItem(flags, object(), hresult, created if destination else None)

        operation.DeleteItem.side_effect = delete
        operation.PerformOperations.side_effect = perform
        operation.GetAnyOperationsAborted.return_value = aborted
        with mock.patch.object(pythoncom, "CoInitialize") as initialize, \
                mock.patch.object(pythoncom, "CoUninitialize") as uninitialize, \
                mock.patch.object(pythoncom, "CoCreateInstance", return_value=operation), \
                mock.patch.object(pythoncom, "WrapObject", side_effect=lambda sink, iid: sink), \
                mock.patch.object(shell, "SHCreateItemFromParsingName", return_value=object()), \
                mock.patch("backend.pc_recycle.file_identity", return_value={"volume": 1, "fileId": 1, "size": 1, "modified": 1}), \
                mock.patch("backend.pc_recycle._capture_receipt", return_value={"verified": True}):
            try:
                recycle_file(path, validate)
            finally:
                initialize.assert_called_once()
                uninitialize.assert_called_once()
        return operation

    def test_request_recycling_and_confirm_created_recycle_destination(self):
        operation = self.run_fake()
        flags = operation.SetOperationFlags.call_args.args[0]
        self.assertTrue(flags & 0x80000)
        self.assertTrue(flags & 0x100000)

    def test_veto_windows_permanent_delete_flags(self):
        with self.assertRaises(Exception):
            self.run_fake(flags=0)

    def test_veto_replaced_original_at_shell_callback(self):
        with self.assertRaises(Exception):
            self.run_fake(validate=lambda: False)

    def test_do_not_report_success_without_bin_destination(self):
        with self.assertRaises(OSError):
            self.run_fake(destination=False)

    def test_aborted_operation_is_not_reported_as_deleted(self):
        with self.assertRaises(OSError):
            self.run_fake(aborted=True)

    def test_native_nonzero_copyengine_recycle_success_hresult(self):
        # Captured from real Windows IFileOperation on disposable fixtures.
        self.run_fake(flags=642, hresult=0x00270008)

    def test_hresult_failure_bit_is_rejected_signed_or_unsigned(self):
        for result in (-2147467259, 0x80004005):
            with self.subTest(result=result), self.assertRaises(OSError):
                self.run_fake(hresult=result)


if __name__ == "__main__":
    unittest.main()
