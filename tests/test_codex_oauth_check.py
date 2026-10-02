# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_oauth, db


class CodexOauthCheckTests(unittest.TestCase):
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

    def test_valid_oauth_uses_read_only_usage_endpoint(self):
        meta = {"name": "codex-valid-free.json", "auth_index": "auth-valid"}
        response = {"status_code": 200, "body": json.dumps({"plan_type": "free"})}

        with patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=meta), patch.object(
            codex_oauth, "_cpa_request_json", return_value=response
        ) as request_json:
            result = codex_oauth.check_cpa_codex_oauth(email="valid@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["http_status"], 200)
        self.assertIsNone(result["error"])
        request_json.assert_called_once()
        method, path, body = request_json.call_args.args
        self.assertEqual((method, path), ("POST", "/v0/management/api-call"))
        self.assertEqual(body["auth_index"], "auth-valid")
        self.assertEqual(body["method"], "GET")
        self.assertEqual(body["url"], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(body["header"]["Authorization"], "Bearer $TOKEN$")
        self.assertNotIn("data", body)

    def test_revoked_oauth_is_invalid_and_preserves_upstream_error(self):
        meta = {"name": "codex-revoked-free.json", "auth_index": "auth-revoked"}
        response = {
            "status_code": 401,
            "body": json.dumps({
                "error": {
                    "message": "Encountered invalidated oauth token for user, failing request",
                    "code": "token_revoked",
                },
                "status": 401,
            }),
        }

        with patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=meta), patch.object(
            codex_oauth, "_cpa_request_json", return_value=response
        ):
            result = codex_oauth.check_cpa_codex_oauth(email="revoked@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["http_status"], 401)
        self.assertIn("invalidated oauth token", result["error"])
        self.assertIn("token_revoked", result["error"])

    def test_forbidden_without_token_error_is_failed_not_invalid(self):
        meta = {"name": "codex-forbidden-free.json", "auth_index": "auth-forbidden"}
        response = {"status_code": 403, "body": json.dumps({"message": "region policy denied"})}

        with patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=meta), patch.object(
            codex_oauth, "_cpa_request_json", return_value=response
        ):
            result = codex_oauth.check_cpa_codex_oauth(email="forbidden@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 403)
        self.assertIn("region policy denied", result["error"])

    def test_forbidden_with_explicit_revocation_is_invalid(self):
        meta = {"name": "codex-revoked-free.json", "auth_index": "auth-revoked"}
        response = {"status_code": 403, "body": json.dumps({"error": {"code": "token_revoked"}})}

        with patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=meta), patch.object(
            codex_oauth, "_cpa_request_json", return_value=response
        ):
            result = codex_oauth.check_cpa_codex_oauth(email="revoked@example.com")

        self.assertEqual(result["status"], "invalid")

    def test_duplicate_email_prefers_latest_active_credential_regardless_of_order(self):
        old = {
            "name": "codex-old-user@example.com-free.json",
            "email": "user@example.com",
            "auth_index": "old",
            "status": "error",
            "unavailable": True,
            "updated_at": "2026-10-01T18:00:00+08:00",
        }
        new = {
            "name": "codex-new-user@example.com-free.json",
            "email": "user@example.com",
            "auth_index": "new",
            "status": "active",
            "unavailable": False,
            "updated_at": "2026-10-01T20:00:00+08:00",
        }

        for files in ([old, new], [new, old]):
            with self.subTest(order=[item["auth_index"] for item in files]), patch.object(
                codex_oauth, "list_cpa_codex_auth_files", return_value=files
            ):
                selected = codex_oauth.find_cpa_codex_auth_file(email="user@example.com")
                self.assertEqual(selected["auth_index"], "new")

    def test_ambiguous_duplicate_credentials_raise_instead_of_random_selection(self):
        files = [
            {"name": "codex-a-user@example.com-free.json", "email": "user@example.com", "auth_index": "a", "status": "active", "updated_at": "2026-10-01T20:00:00+08:00"},
            {"name": "codex-b-user@example.com-free.json", "email": "user@example.com", "auth_index": "b", "status": "active", "updated_at": "2026-10-01T20:00:00+08:00"},
        ]
        with patch.object(codex_oauth, "list_cpa_codex_auth_files", return_value=files):
            with self.assertRaisesRegex(RuntimeError, "无法消歧"):
                codex_oauth.find_cpa_codex_auth_file(email="user@example.com")

    def test_missing_credential_does_not_call_api(self):
        with patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=None), patch.object(
            codex_oauth, "_cpa_request_json"
        ) as request_json:
            result = codex_oauth.check_cpa_codex_oauth(email="missing@example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "missing")
        self.assertIsNone(result["http_status"])
        request_json.assert_not_called()

    def test_management_failure_is_left_for_service_to_classify(self):
        meta = {"name": "codex-error-free.json", "auth_index": "auth-error"}
        with patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=meta), patch.object(
            codex_oauth, "_cpa_request_json", side_effect=RuntimeError("CPA timeout")
        ):
            with self.assertRaisesRegex(RuntimeError, "CPA timeout"):
                codex_oauth.check_cpa_codex_oauth(email="error@example.com")

    def test_db_result_does_not_overwrite_historical_codex_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([{
                "id": 8,
                "email": "revoked@example.com",
                "codex_status": "success",
                "codex_error": "historical message",
            }]), encoding="utf-8")

            with patch.multiple(db, **self._storage_patches(root)):
                self.assertTrue(db.claim_account_codex_oauth_check(8))
                check_id = db.get_account(8)["codex_oauth_check_id"]
                self.assertTrue(db.mark_account_codex_oauth_check_running(8, expected_check_id=check_id))
                self.assertTrue(db.update_account_codex_oauth_check(8, {
                    "ok": False,
                    "status": "invalid",
                    "http_status": 401,
                    "error": "token_revoked",
                    "checked_at": "2026-10-01T00:00:00+00:00",
                    "cpa_name": "codex-revoked-free.json",
                }, expected_check_id=check_id))
                account = db.get_account(8)

            self.assertEqual(account["codex_status"], "success")
            self.assertEqual(account["codex_error"], "historical message")
            self.assertEqual(account["codex_oauth_check_status"], "invalid")
            self.assertFalse(account["codex_oauth_check_ok"])
            self.assertEqual(account["codex_oauth_check_http_status"], 401)
            self.assertEqual(account["codex_oauth_check_error"], "token_revoked")

    def test_stale_worker_cannot_overwrite_newer_check_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([{
                "id": 8,
                "email": "user@example.com",
            }]), encoding="utf-8")

            with patch.multiple(db, **self._storage_patches(root)), patch.object(
                db, "_PLAN_CHECK_STALE_SECONDS", -1
            ), patch.object(db, "_PLAN_CHECK_QUEUE_STALE_SECONDS", -1):
                self.assertTrue(db.claim_account_codex_oauth_check(8))
                old_id = db.get_account(8)["codex_oauth_check_id"]
                self.assertTrue(db.mark_account_codex_oauth_check_running(8, expected_check_id=old_id))
                self.assertTrue(db.claim_account_codex_oauth_check(8))
                new_id = db.get_account(8)["codex_oauth_check_id"]
                self.assertNotEqual(old_id, new_id)
                self.assertTrue(db.mark_account_codex_oauth_check_running(8, expected_check_id=new_id))
                self.assertTrue(db.update_account_codex_oauth_check(8, {
                    "ok": True,
                    "status": "valid",
                    "http_status": 200,
                }, expected_check_id=new_id))
                self.assertFalse(db.update_account_codex_oauth_check(8, {
                    "ok": False,
                    "status": "invalid",
                    "http_status": 401,
                    "error": "stale revoked result",
                }, expected_check_id=old_id))
                account = db.get_account(8)

            self.assertEqual(account["codex_oauth_check_status"], "valid")
            self.assertEqual(account["codex_oauth_check_http_status"], 200)


if __name__ == "__main__":
    unittest.main()
