import unittest
from unittest.mock import Mock, call, patch

from core import plan_check_service


class PlanCheckSameProxyTests(unittest.TestCase):
    def test_registration_recheck_reuses_explicit_proxy(self):
        proxy = "http://sid-abc123:bridge@127.0.0.1:25001"
        first = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }
        second = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }
        slots = Mock()
        with patch.object(plan_check_service, "_QUEUE_SLOTS", slots), \
             patch.object(
                 plan_check_service.db,
                 "mark_account_plan_check_running",
                 return_value=True,
             ), \
             patch.object(plan_check_service.db, "update_account_plan_check"), \
             patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(
                 plan_check_service,
                 "_registration_recheck_delay",
                 return_value=2.0,
             ), \
             patch(
                 "core.roxybrowser_client.prepare_proxy_for_roxy",
                 return_value=proxy,
             ) as prepare, \
             patch.object(plan_check_service.time, "sleep") as sleep, \
             patch.object(
                 plan_check_service,
                 "check_account_plan",
                 side_effect=[first, second],
             ) as check:
            result = plan_check_service._run_plan_check(
                account_id=42,
                email="user@example.com",
                access_token="token-value",
                trigger="registration_auto",
                proxy=proxy,
                timezone_offset_min="-",
            )

        self.assertEqual(result, second)
        prepare.assert_called_once_with(proxy)
        self.assertEqual(
            check.call_args_list,
            [
                call("token-value", proxy=proxy, timezone_offset_min="-"),
                call(
                    "token-value",
                    proxy=proxy,
                    timezone_offset_min="-",
                    max_attempts=1,
                ),
            ],
        )
        sleep.assert_called_once_with(2.0)
        slots.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
