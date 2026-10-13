"""Drive retention cleanup must survive transient errors on the listing call.

Incident 2026-10-13 (backup_equiredes): a Drive HttpError 500 on files().list()
inside cleanup_old_files aborted a rule whose uploads were already verified.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core import gdrive

try:
    import httplib2
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover - libs are in requirements.txt
    HttpError = None


def _http_error(status: int) -> "HttpError":
    return HttpError(httplib2.Response({"status": status}), b"Internal Error")


def _make_uploader(svc) -> "gdrive.DriveUploader":
    """DriveUploader wired to a fake service, without credentials or network."""
    uploader = object.__new__(gdrive.DriveUploader)
    uploader._resolve_target_folder = lambda: "folder123"
    uploader._get_service = lambda: svc
    return uploader


@unittest.skipIf(HttpError is None, "google libs not installed")
class CleanupOldFilesRetryTest(unittest.TestCase):

    def setUp(self):
        # No real backoff waits during tests.
        patcher = mock.patch.object(gdrive.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _service(self, list_side_effect):
        svc = mock.MagicMock()
        svc.files.return_value.list.return_value.execute.side_effect = list_side_effect
        return svc

    def test_listing_retries_transient_500(self):
        old = (datetime.now(tz=timezone.utc) - timedelta(days=120)).isoformat()
        page = {"files": [{"id": "f1", "name": "odoo_db_old.sql", "createdTime": old}]}
        svc = self._service([_http_error(500), page])

        deleted = _make_uploader(svc).cleanup_old_files("odoo_db_", 90)

        self.assertEqual(deleted, 1)
        self.assertEqual(svc.files.return_value.list.return_value.execute.call_count, 2)

    def test_listing_non_retryable_error_still_raises(self):
        svc = self._service([_http_error(403)])

        with self.assertRaises(HttpError):
            _make_uploader(svc).cleanup_old_files("odoo_db_", 90)


if __name__ == "__main__":
    unittest.main()
