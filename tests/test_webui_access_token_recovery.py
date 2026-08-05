# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiAccessTokenRecoveryTests(unittest.TestCase):
    def setUp(self):
        with patch("webui.app.db.recover_interrupted_access_token_recoveries", return_value=0, create=True), \
             patch("webui.app.db.recover_interrupted_extract_links", return_value={
                 "failed_count": 0,
                 "kakao_batches": [],
             }), \
             patch("webui.app.extract_link_service.resume_interrupted_kakao_batches", return_value={
                 "resumed_batches": 0,
                 "failed_batches": 0,
             }):
            self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_single_recovery_passes_force(self):
        with patch("webui.app.access_token_recovery_service.enqueue_account_access_token_recovery", return_value={
            "accepted": True,
            "busy": False,
            "skipped": False,
            "account_id": 168,
            "email": "invalid@example.com",
        }) as enqueue:
            response = self.client.post(
                "/api/accounts/recover-access-token",
                json={"account_id": 168, "force": True},
            )

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once_with(account_id=168, trigger="manual", force=True)

    def test_bulk_recovery_passes_force_to_every_account(self):
        with patch(
            "webui.app.access_token_recovery_service.enqueue_account_access_token_recovery",
            side_effect=[
                {"accepted": True, "busy": False, "skipped": False, "account_id": 168},
                {"accepted": True, "busy": False, "skipped": False, "account_id": 167},
            ],
        ) as enqueue:
            response = self.client.post(
                "/api/accounts/recover-access-token-bulk",
                json={"account_ids": [168, 167], "force": True},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_count, 2)
        self.assertTrue(all(call.kwargs["force"] for call in enqueue.call_args_list))

    def test_bulk_recovery_classifies_started_busy_and_skipped(self):
        results = [
            {"accepted": True, "busy": False, "skipped": False, "account_id": 1},
            {"accepted": False, "busy": True, "skipped": False, "error": "正在补 AT"},
            {"accepted": False, "busy": False, "skipped": True, "error": "已有 access_token"},
        ]
        with patch(
            "webui.app.access_token_recovery_service.enqueue_account_access_token_recovery",
            side_effect=results,
        ):
            response = self.client.post(
                "/api/accounts/recover-access-token-bulk",
                json={"account_ids": [1, 2, 3]},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["busy_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)

    def test_stop_bulk_and_log(self):
        with patch("webui.app.access_token_recovery_service.request_stop_bulk", return_value={
            "stopped": [{"id": 1}],
            "stopped_count": 1,
            "skipped": [],
            "skipped_count": 0,
        }):
            stopped = self.client.post(
                "/api/accounts/recover-access-token/stop-bulk",
                json={"account_ids": [1]},
            )
        with patch("webui.app.db.get_account", return_value={"id": 1, "email": "missing@example.com"}), \
             patch("webui.app.access_token_recovery_service.read_log", return_value="recovery log"):
            log = self.client.get("/api/accounts/recover-access-token/1/log")

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(log.get_json()["log"], "recovery log")

    def test_compact_account_exposes_status_without_log_path(self):
        account = {
            "id": 1,
            "email": "invalid@example.com",
            "access_token": "TOKEN_INVALID",
            "plan_check_ok": False,
            "plan_check_error": "HTTP 401",
            "at_recovery_status": "failed",
            "at_recovery_error": "OTP 超时",
            "at_recovery_log_file": "C:/private/recovery.log",
        }
        with patch("webui.app.db.list_accounts_page", return_value={
                 "items": [account],
                 "total": 1,
                 "offset": 0,
                 "limit": 50,
                 "revision": "1",
             }), \
             patch("webui.app.db.is_account_access_token_invalid", return_value=True):
            response = self.client.get("/api/accounts?paged=1")

        item = response.get_json()["items"][0]
        self.assertEqual(item["at_recovery_status"], "failed")
        self.assertTrue(item["access_token_invalid"])
        self.assertTrue(item["has_at_recovery_log"])
        self.assertNotIn("at_recovery_log_file", item)


if __name__ == "__main__":
    unittest.main()
