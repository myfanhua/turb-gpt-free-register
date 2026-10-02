# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class WebUiCodexOauthCheckBulkTests(unittest.TestCase):
    @staticmethod
    def _storage_patches(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex_accounts",
            "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
            "_VIEWER_HTML": root / "viewer.html",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
        }

    def test_bulk_check_deduplicates_reports_results_and_hides_internal_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([
                {"id": 1, "email": "valid@example.com"},
                {"id": 2, "email": ""},
                {"id": 3, "email": "busy@example.com"},
                {"id": 4, "email": "failed@example.com"},
            ]), encoding="utf-8")

            with patch.multiple(db, **self._storage_patches(root)):
                app = create_app(auth_code="test-auth")
                client = app.test_client()

                def enqueue(**kwargs):
                    if kwargs["account_id"] == 3:
                        return {"accepted": False, "busy": True, "error": "正在检测", "auth_index": "secret"}
                    if kwargs["account_id"] == 4:
                        return {"accepted": False, "busy": False, "error": "CPA timeout", "future": object()}
                    return {"accepted": True, "busy": False, "auth_index": "secret", "future": object()}

                with patch(
                    "core.codex_oauth_check_service.enqueue_account_codex_oauth_check",
                    side_effect=enqueue,
                ) as mocked:
                    response = client.post(
                        "/api/accounts/check-codex-oauth-bulk",
                        json={"account_ids": [1, 1, 2, 3, 4, 99, "bad"]},
                        headers={"X-Auth-Code": "test-auth"},
                    )

                self.assertEqual(response.status_code, 202)
                body = response.get_json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["started_count"], 1)
                self.assertEqual(body["started"], [{"id": 1, "email": "valid@example.com", "status": "queued"}])
                self.assertEqual(body["busy_count"], 1)
                self.assertEqual(body["failed_count"], 1)
                self.assertEqual(body["failed"], [{"id": 4, "email": "failed@example.com", "error": "CPA timeout"}])
                self.assertEqual(
                    {(item["id"], item["reason"]) for item in body["skipped"]},
                    {
                        (2, "邮箱为空"),
                        (3, "正在检测"),
                        (99, "账号不存在"),
                        ("bad", "ID 非法"),
                    },
                )
                self.assertEqual([call.kwargs["account_id"] for call in mocked.call_args_list], [1, 3, 4])
                serialized = json.dumps(body, ensure_ascii=False)
                self.assertNotIn("auth_index", serialized)
                self.assertNotIn("future", serialized)

    def test_bulk_check_validates_empty_and_maximum(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                app = create_app(auth_code="test-auth")
                client = app.test_client()

                empty = client.post(
                    "/api/accounts/check-codex-oauth-bulk",
                    json={"account_ids": []},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(empty.status_code, 400)

                too_many = client.post(
                    "/api/accounts/check-codex-oauth-bulk",
                    json={"account_ids": list(range(501))},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(too_many.status_code, 400)


if __name__ == "__main__":
    unittest.main()
