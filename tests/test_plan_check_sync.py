import unittest
from datetime import datetime
from unittest.mock import patch

from core import plan_check_service


class PlanCheckSyncTests(unittest.TestCase):
    def test_check_account_plan_now_starts_saved_local_proxy_bridge(self):
        proxy = "http://sid-account168:bridge@127.0.0.1:25001"
        result = {
            "ok": True,
            "current_plan_type": "free",
            "checked_at": "2026-08-04T12:00:00",
        }
        with patch.object(plan_check_service.proxy_cfg, "PROXY_CHAIN_ENABLED", True), \
             patch.object(plan_check_service.proxy_cfg, "PROXY_CHAIN_LISTEN_HOST", "127.0.0.1"), \
             patch.object(plan_check_service.proxy_cfg, "PROXY_CHAIN_LISTEN_PORT", 25001), \
             patch("core.roxybrowser_client.prepare_proxy_for_roxy", return_value=proxy) as prepare, \
             patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(plan_check_service, "check_account_plan", return_value=result) as check, \
             patch.object(plan_check_service.db, "update_account_plan_check"):
            actual = plan_check_service.check_account_plan_now(
                account_id=168,
                email="account168@example.com",
                access_token="token-value",
                proxy=proxy,
            )

        self.assertEqual(actual, result)
        prepare.assert_called_once_with(proxy)
        check.assert_called_once_with("token-value", proxy=proxy, timezone_offset_min="-")

    def test_check_account_plan_now_uses_rate_limit_and_persists_result(self):
        result = {
            "ok": True,
            "current_plan_type": "plus",
            "checked_at": "2026-08-03T12:00:00",
        }
        with patch.object(plan_check_service, "_wait_for_rate_slot") as wait, \
             patch.object(plan_check_service, "check_account_plan", return_value=result) as check, \
             patch.object(plan_check_service.db, "update_account_plan_check") as update:
            actual = plan_check_service.check_account_plan_now(
                account_id=42,
                email="plus@example.com",
                access_token="token-value",
                trigger="codex_retry_gate",
            )

        self.assertEqual(actual, result)
        wait.assert_called_once_with()
        check.assert_called_once_with("token-value", proxy=None, timezone_offset_min="-")
        update.assert_called_once_with(acc_id=42, result=result)

    def test_check_account_plan_now_returns_and_persists_structured_failure(self):
        with patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(
                 plan_check_service,
                 "check_account_plan",
                 side_effect=RuntimeError("network down"),
             ), \
             patch.object(plan_check_service.db, "update_account_plan_check") as update:
            actual = plan_check_service.check_account_plan_now(
                account_id=42,
                email="plus@example.com",
                access_token="token-value",
            )

        self.assertFalse(actual["ok"])
        datetime.fromisoformat(actual["checked_at"])
        self.assertIn("RuntimeError: network down", actual["error"])
        update.assert_called_once_with(acc_id=42, result=actual)

    def test_check_account_plan_now_logs_database_write_failure_and_returns_result(self):
        result = {"ok": True, "current_plan_type": "plus"}
        with patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(plan_check_service, "check_account_plan", return_value=result), \
             patch.object(
                 plan_check_service.db,
                 "update_account_plan_check",
                 side_effect=OSError("database unavailable"),
             ), \
             self.assertLogs(plan_check_service.logger, level="ERROR") as logs:
            actual = plan_check_service.check_account_plan_now(
                account_id=42,
                email="plus@example.com",
                access_token="token-value",
            )

        self.assertIs(actual, result)
        self.assertIn("写入同步查询结果失败", "\n".join(logs.output))

    def test_check_account_plan_now_returns_failure_when_result_is_not_persisted(self):
        result = {
            "ok": True,
            "current_plan_type": "plus",
            "checked_at": "2026-08-03T12:00:00",
        }
        with patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(plan_check_service, "check_account_plan", return_value=result), \
             patch.object(
                 plan_check_service.db,
                 "update_account_plan_check",
                 return_value=False,
             ) as update, \
             patch.object(plan_check_service.logger, "error") as log_error:
            actual = plan_check_service.check_account_plan_now(
                account_id=42,
                email="plus@example.com",
                access_token="token-value",
            )

        self.assertFalse(actual["ok"])
        self.assertEqual(actual["checked_at"], result["checked_at"])
        self.assertIn("套餐结果写回失败", actual["error"])
        self.assertIn("账号不存在", actual["error"])
        update.assert_called_once_with(acc_id=42, result=result)
        log_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
