import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_retry_service


class CodexRetryPlusGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir_handle = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_handle.name)
        self._clear_retry_state()

    def tearDown(self):
        self._clear_retry_state()
        self.temp_dir_handle.cleanup()

    @staticmethod
    def _clear_retry_state():
        with codex_retry_service._RETRYING_LOCK:
            codex_retry_service._RETRYING.clear()
            codex_retry_service._STOP_REQUESTED.clear()
            codex_retry_service._RUNNING_THREADS.clear()
            codex_retry_service._RESERVED_AT.clear()

    def _run_worker(self, email, account, plan_result):
        self.assertTrue(codex_retry_service.reserve(email))
        with patch.object(
            codex_retry_service.db,
            "get_account_by_email",
            return_value=account,
        ), patch(
            "core.plan_check_service.check_account_plan_now",
            return_value=plan_result,
        ) as plan_check, patch(
            "core.codex_oauth.run_codex_oauth",
            return_value={"status": "success", "ok": True},
        ) as oauth, patch.object(
            codex_retry_service.db,
            "update_account_codex_status",
        ) as update, patch(
            "config.reload_all",
        ):
            result = codex_retry_service.run_worker(
                email,
                target_log_path=self.temp_dir / f"{email}.log",
            )
        return result, plan_check, oauth, update

    def test_active_plus_plan_variants_pass(self):
        self.assertTrue(codex_retry_service._is_actual_plus({"current_plan_type": "plus"}))
        self.assertTrue(codex_retry_service._is_actual_plus({"current_plan_type": "chatgpt_plus"}))
        self.assertTrue(codex_retry_service._is_actual_plus({"plan_type": "CHATGPT_PLUS"}))

    def test_trial_eligible_free_and_other_plans_do_not_pass(self):
        self.assertFalse(codex_retry_service._is_actual_plus({
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }))
        self.assertFalse(codex_retry_service._is_actual_plus({
            "current_plan_type": "free_plus_trial",
        }))
        for plan in ("pro", "team", "go", "unknown", ""):
            with self.subTest(plan=plan):
                self.assertFalse(codex_retry_service._is_actual_plus({"current_plan_type": plan}))

    def test_plus_plan_variants_run_oauth_after_live_gate(self):
        for index, plan in enumerate(("plus", "chatgpt_plus"), start=1):
            with self.subTest(plan=plan):
                email = f"plus-{index}@example.com"
                account = {"id": index, "email": email, "access_token": "token-value"}
                result, plan_check, oauth, update = self._run_worker(
                    email,
                    account,
                    {"ok": True, "current_plan_type": plan},
                )

                self.assertTrue(result["ok"])
                plan_check.assert_called_once_with(
                    account_id=index,
                    email=email,
                    access_token="token-value",
                    trigger="codex_retry_gate",
                )
                oauth.assert_called_once_with(email, force=True)
                update.assert_called_once_with(email, "success", None)
                self.assertFalse(codex_retry_service.is_retrying(email))

    def test_non_plus_worker_stops_before_oauth_updates_status_and_releases_reserve(self):
        email = "free@example.com"
        account = {"id": 7, "email": email, "access_token": "token"}
        result, plan_check, oauth, update = self._run_worker(
            email,
            account,
            {"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertEqual(result, {
            "status": "skipped",
            "ok": False,
            "message": "当前未开通 Plus，未执行邮箱 OTP 和接码",
        })
        plan_check.assert_called_once()
        oauth.assert_not_called()
        update.assert_called_once_with(email, "skipped", result["message"])
        self.assertFalse(codex_retry_service.is_retrying(email))

    def test_plan_query_failure_stops_before_oauth_updates_status_and_releases_reserve(self):
        email = "query-failed@example.com"
        account = {"id": 8, "email": email, "access_token": "token"}
        result, plan_check, oauth, update = self._run_worker(
            email,
            account,
            {"ok": False, "error": "network down"},
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Plus 前置检验失败：network down")
        plan_check.assert_called_once()
        oauth.assert_not_called()
        update.assert_called_once_with(email, "failed", result["message"])
        self.assertFalse(codex_retry_service.is_retrying(email))

    def test_missing_token_stops_before_plan_query_and_oauth_and_releases_reserve(self):
        email = "missing-token@example.com"
        account = {"id": 9, "email": email, "access_token": "  "}
        result, plan_check, oauth, update = self._run_worker(email, account, None)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["message"], "Plus 前置检验失败：账号缺少 access_token")
        plan_check.assert_not_called()
        oauth.assert_not_called()
        update.assert_called_once_with(email, "failed", result["message"])
        self.assertFalse(codex_retry_service.is_retrying(email))

    def test_missing_account_gate_returns_failed_without_plan_query(self):
        with patch.object(codex_retry_service.db, "get_account_by_email", return_value=None), patch(
            "core.plan_check_service.check_account_plan_now",
        ) as plan_check:
            result = codex_retry_service._check_plus_gate("missing@example.com")

        self.assertEqual(result, {
            "ok": False,
            "status": "failed",
            "message": "Plus 前置检验失败：账号不存在",
        })
        plan_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
