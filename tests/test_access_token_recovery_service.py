# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import access_token_recovery_service as service


class AccessTokenRecoveryServiceTests(unittest.TestCase):
    def test_worker_relogs_existing_invalid_token_and_passes_previous_token(self):
        account = {
            "id": 7,
            "email": "invalid@example.com",
            "access_token": "TOKEN_OLD_INVALID",
            "proxy_used": "http://saved-proxy",
            "device_id": "device-7",
            "plan_check_ok": False,
            "plan_check_error": "HTTP 401",
        }
        with patch.object(service.db, "get_account", return_value=account), \
             patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
             patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
             patch.object(service, "run_roxy_access_token_recovery", return_value={
                 "session_info": {"accessToken": "TOKEN_NEW"},
                 "device_id": "device-7",
                 "proxy_used": "http://saved-proxy",
             }) as run, \
             patch.object(service.db, "complete_account_access_token_recovery", return_value={
                 "updated": True,
                 "replaced": True,
             }) as complete:
            result = service._run_recovery(
                account_id=7,
                trigger="auto_invalid",
                force=False,
            )

        self.assertTrue(result["ok"])
        run.assert_called_once()
        self.assertEqual(
            complete.call_args.kwargs["previous_access_token"],
            "TOKEN_OLD_INVALID",
        )

    def test_worker_uses_saved_proxy_and_generates_missing_device(self):
        account = {
            "id": 7,
            "email": "missing@example.com",
            "access_token": "",
            "proxy_used": "http://saved-proxy",
            "device_id": "",
        }
        with patch.object(service.db, "get_account", return_value=account), \
             patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
             patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
             patch.object(service, "run_roxy_access_token_recovery", return_value={
                 "session_info": {"accessToken": "TOKEN_NEW"},
                 "device_id": "generated-device",
                 "proxy_used": "http://saved-proxy",
             }) as run, \
             patch.object(service.db, "complete_account_access_token_recovery", return_value={
                 "updated": True,
                 "already_present": False,
             }) as complete:
            result = service._run_recovery(account_id=7, trigger="manual")

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.kwargs["proxy"], "http://saved-proxy")
        self.assertTrue(run.call_args.kwargs["device_id"])
        complete.assert_called_once()

    def test_same_token_failure_is_persisted_without_exposing_token(self):
        account = {
            "id": 8,
            "email": "invalid@example.com",
            "access_token": "TOKEN_OLD_INVALID",
            "proxy_used": "http://saved-proxy",
            "device_id": "device-8",
        }
        with patch.object(service.db, "get_account", return_value=account), \
             patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
             patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
             patch.object(service, "run_roxy_access_token_recovery", return_value={
                 "session_info": {"accessToken": "TOKEN_OLD_INVALID"},
                 "device_id": "device-8",
                 "proxy_used": "http://saved-proxy",
             }), \
             patch.object(
                 service.db,
                 "complete_account_access_token_recovery",
                 side_effect=ValueError("登录未返回新的 Access Token"),
             ), \
             patch.object(service.db, "fail_account_access_token_recovery") as fail:
            result = service._run_recovery(
                account_id=8,
                trigger="manual",
                force=True,
            )

        self.assertFalse(result["ok"])
        self.assertIn("登录未返回新的 Access Token", result["error"])
        self.assertNotIn("TOKEN_OLD_INVALID", str(result))
        fail.assert_called_once()

    def test_enqueue_passes_force_to_claim_and_worker(self):
        fake_future = object()
        with patch.object(service._QUEUE_SLOTS, "acquire", return_value=True), \
             patch.object(service.db, "claim_account_access_token_recovery", return_value={
                 "accepted": True,
                 "busy": False,
                 "skipped": False,
             }) as claim, \
             patch.object(service, "_event_for"), \
             patch.object(service._EXECUTOR, "submit", return_value=fake_future) as submit:
            result = service.enqueue_account_access_token_recovery(
                account_id=9,
                trigger="manual",
                force=True,
            )

        self.assertIs(result["future"], fake_future)
        self.assertTrue(claim.call_args.kwargs["force"])
        self.assertTrue(submit.call_args.kwargs["force"])

    def test_failure_is_sanitized_before_persisting(self):
        account = {
            "id": 8,
            "email": "missing@example.com",
            "access_token": "",
            "proxy_used": "http://user:secret@proxy.example:8080",
            "device_id": "device-8",
        }
        with self.assertLogs(service.logger, level="ERROR") as captured, \
             patch.object(service.db, "get_account", return_value=account), \
             patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
             patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
             patch.object(service, "run_roxy_access_token_recovery", side_effect=RuntimeError(
                 "authorization=Bearer "
                 + "eyJhb"
                 + "GciOi.secret.signature"
                 + " via http://user:secret@proxy.example:8080"
             )), \
             patch.object(service.db, "fail_account_access_token_recovery") as fail:
            result = service._run_recovery(account_id=8, trigger="manual")

        self.assertFalse(result["ok"])
        persisted = fail.call_args.kwargs["error"]
        self.assertNotIn("eyJhbGciOi", persisted)
        self.assertNotIn("user:secret", persisted)
        logged = "\n".join(captured.output)
        self.assertNotIn("eyJhbGciOi", logged)
        self.assertNotIn("user:secret", logged)

    def test_stop_sets_event_and_database_flag(self):
        with patch.object(service.db, "request_account_access_token_recovery_stop", return_value={
            "stopped": True,
            "running": True,
            "status": "running",
        }):
            result = service.request_stop(9)
        self.assertTrue(result["stopped"])


if __name__ == "__main__":
    unittest.main()
