# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from core import assurivo_mail_client as assurivo
from core import db, email_provider


class AssurivoGenericMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.generic_json = root / "generic.json"
        self.generic_txt = root / "generic.txt"
        self.material = root / "assurivo.txt"
        self.state = root / "assurivo.json"
        self.url = "https://assurivo.com/console/open.php?mail=a%40example.com&pwd=secret-query-code&limit=5"
        self.patches = [
            patch.object(db, "_GENERIC_API_EMAIL_JSON", self.generic_json),
            patch.object(db, "_GENERIC_API_EMAIL_TXT", self.generic_txt),
            patch.object(email_config, "ASSURIVO_ACCOUNTS_FILE", str(self.material)),
        ]
        for item in self.patches:
            item.start()
        assurivo._CONTEXT_CACHE.clear()

    def tearDown(self):
        assurivo._CONTEXT_CACHE.clear()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_generic_import_routes_complete_assurivo_url_to_independent_pool(self):
        inserted, skipped = db.import_generic_api_emails([{"email": "a@example.com", "code_url": self.url}])

        self.assertEqual((inserted, skipped), (1, 0))
        self.assertEqual(db.generic_api_email_pool_summary()["total"], 0)
        account = assurivo.get_account_context("a@example.com")
        self.assertIsNotNone(account)
        self.assertEqual(account.query_url, self.url)

    def test_legacy_migration_preserves_used_state_and_removes_generic_count(self):
        db._write_json(db._GENERIC_API_EMAIL_JSON, [{
            "id": 1, "email": "a@example.com", "code_url": self.url,
            "status": "used", "used_at": "2026-07-25T04:49:00", "note": "in progress",
        }])

        result = assurivo.migrate_legacy_generic_api_records()

        self.assertEqual(result["migrated"], 1)
        self.assertEqual(db.generic_api_email_pool_summary()["total"], 0)
        row = assurivo.list_pool()[0]
        self.assertEqual(row["email"], "a@example.com")
        self.assertEqual(row["status"], "used")
        self.assertNotIn("query_url", row)

    @patch("core.assurivo_mail_client.fetch_latest_otp", return_value="654321")
    def test_wait_for_otp_migrates_then_dispatches_to_assurivo(self, fetch):
        db._write_json(db._GENERIC_API_EMAIL_JSON, [{
            "id": 1, "email": "a@example.com", "code_url": self.url,
            "status": "used", "used_at": "2026-07-25T04:49:00", "note": None,
        }])
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("a@example.com", after_ts=100.0), "654321")

        fetch.assert_called_once_with("a@example.com", after_ts=100.0)
        self.assertEqual(db.generic_api_email_pool_summary()["total"], 0)


if __name__ == "__main__":
    unittest.main()
