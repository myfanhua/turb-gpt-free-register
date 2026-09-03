# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import _account_secret_value, _compact_account_for_list


class AccountTxtExportTests(unittest.TestCase):
    def test_download_txt_line_has_exact_four_fields(self):
        row = {
            "id": 7,
            "email": "person@example.com",
            "email_source": "generic_api",
            "original_email_line": "person@example.com----https://mail.example/api/messages?key=fixture",
            "extra_json": json.dumps({"registration_password": "Password!123"}),
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }

        line = _account_secret_value(row, "download_txt_line")

        self.assertEqual(
            line.split("----"),
            [
                "person@example.com",
                "Password!123",
                "JBSWY3DPEHPK3PXP",
                "https://mail.example/api/messages?key=fixture",
            ],
        )

    def test_download_txt_line_keeps_missing_password_and_2fa_empty(self):
        row = {
            "id": 8,
            "email": "empty@example.com",
            "email_source": "generic_api",
            "original_email_line": "empty@example.com----https://mail.example/api/empty",
            "extra_json": {},
            "totp_secret": "",
        }

        line = _account_secret_value(row, "download_txt_line")

        self.assertEqual(
            line.split("----"),
            ["empty@example.com", "", "", "https://mail.example/api/empty"],
        )
        self.assertNotIn("未设置", line)

    def test_download_txt_line_recovers_api_address_from_email_resource(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            generic_path = root / "generic.json"
            generic_path.write_text(
                json.dumps([
                    {
                        "id": 3,
                        "email": "rebound@example.com",
                        "code_url": "https://mail.example/api/rebound",
                        "registered_account_id": 9,
                        "status": "used",
                    }
                ]),
                encoding="utf-8",
            )
            with patch.object(db, "_GENERIC_API_EMAIL_JSON", generic_path):
                line = _account_secret_value(
                    {
                        "id": 9,
                        "email": "rebound@example.com",
                        "email_source": "generic_api",
                        "original_email_line": "rebound@example.com",
                        "extra_json": {"registration_password": "fixture-pass"},
                    },
                    "download_txt_line",
                )

        self.assertEqual(
            line.split("----"),
            ["rebound@example.com", "fixture-pass", "", "https://mail.example/api/rebound"],
        )


class AccountRegistrationCountryTests(unittest.TestCase):
    def test_compact_account_includes_registration_country_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_path = root / "jobs.json"
            jobs_path.write_text(
                json.dumps([
                    {
                        "id": 21,
                        "job_type": "registration",
                        "status": "success",
                        "account_id": 5,
                        "email": "country@example.com",
                        "proxy_provider": "cliproxy_traffic",
                        "proxy_country": "id",
                        "completed_at": "2026-08-30T10:00:00",
                    }
                ]),
                encoding="utf-8",
            )
            with patch.object(db, "_JOBS_JSON", jobs_path), patch.object(
                db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"
            ):
                compact = _compact_account_for_list(
                    {"id": 5, "email": "country@example.com", "note": "fixture"}
                )

        self.assertEqual(compact["registration_proxy_country"], "ID")
        self.assertEqual(compact["registration_proxy_provider"], "cliproxy_traffic")

    def test_compact_account_recovers_legacy_country_from_saved_proxy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_path = root / "jobs.json"
            jobs_path.write_text("[]", encoding="utf-8")
            with patch.object(db, "_JOBS_JSON", jobs_path), patch.object(
                db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"
            ):
                compact = _compact_account_for_list(
                    {
                        "id": 6,
                        "email": "legacy@example.com",
                        "proxy_used": "socks5h://fixture-user-region-JP-sid-test-t-30:fixture-pass@proxy.example:3000",
                    }
                )

        self.assertEqual(compact["registration_proxy_country"], "JP")
        self.assertEqual(compact["registration_proxy_provider"], "cliproxy_traffic")


if __name__ == "__main__":
    unittest.main()
