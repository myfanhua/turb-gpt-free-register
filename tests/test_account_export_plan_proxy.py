import unittest
from unittest.mock import patch

from core.account_export import save_account_data


class AccountExportPlanProxyTests(unittest.TestCase):
    def test_registration_auto_plan_check_receives_saved_proxy(self):
        registration_proxy = "http://sid-abc123:bridge@127.0.0.1:25001"
        with patch("core.db.insert_account", return_value=42), \
             patch("core.account_export._append_batch_archive", return_value="batch"), \
             patch(
                 "core.plan_check_service.enqueue_account_plan_check",
                 return_value={"accepted": True},
             ) as enqueue:
            row_id = save_account_data(
                email="user@example.com",
                access_token="token-value",
                proxy_used=registration_proxy,
                extra={"account": {"planType": "free"}},
            )

        self.assertEqual(row_id, 42)
        enqueue.assert_called_once_with(
            account_id=42,
            email="user@example.com",
            access_token="token-value",
            trigger="registration_auto",
            proxy=registration_proxy,
        )

    def test_registration_auto_keeps_default_route_when_proxy_missing(self):
        with patch("core.db.insert_account", return_value=43), \
             patch("core.account_export._append_batch_archive", return_value="batch"), \
             patch(
                 "core.plan_check_service.enqueue_account_plan_check",
                 return_value={"accepted": True},
             ) as enqueue:
            save_account_data(
                email="direct@example.com",
                access_token="token-value",
                proxy_used=None,
                extra={},
            )

        enqueue.assert_called_once_with(
            account_id=43,
            email="direct@example.com",
            access_token="token-value",
            trigger="registration_auto",
            proxy=None,
        )


if __name__ == "__main__":
    unittest.main()
