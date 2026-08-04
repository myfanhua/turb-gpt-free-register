import unittest
from unittest.mock import patch

from core.account_export import save_account_data


class AccountExportPlanProxyTests(unittest.TestCase):
    def test_registration_auto_plan_check_receives_saved_proxy(self):
        registration_proxy = "http://sid-abc123:bridge@127.0.0.1:25001"
        registration_location = {
            "country_code": "US",
            "country": "United States",
            "region": "California",
            "ip": "203.0.113.10",
        }
        with patch("core.db.insert_account", return_value=42) as insert, \
             patch("core.account_export._append_batch_archive", return_value="batch"), \
             patch(
                 "core.account_export.lookup_registration_location",
                 return_value=registration_location,
             ) as lookup, \
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
        lookup.assert_called_once_with(registration_proxy)
        insert.assert_called_once_with(
            email="user@example.com",
            access_token="token-value",
            totp_secret=None,
            user_id=None,
            user_name=None,
            plan_type="free",
            expires_at=None,
            device_id=None,
            proxy_used=registration_proxy,
            email_source=None,
            extra={"account": {"planType": "free"}},
            codex_status=None,
            codex_error=None,
            registration_country_code="US",
            registration_country="United States",
            registration_region="California",
            registration_ip="203.0.113.10",
        )
        enqueue.assert_called_once_with(
            account_id=42,
            email="user@example.com",
            access_token="token-value",
            trigger="registration_auto",
            proxy=registration_proxy,
        )

    def test_registration_auto_keeps_default_route_when_proxy_missing(self):
        with patch("core.db.insert_account", return_value=43) as insert, \
             patch("core.account_export._append_batch_archive", return_value="batch"), \
             patch(
                 "core.account_export.lookup_registration_location",
                 return_value={},
             ) as lookup, \
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

        lookup.assert_called_once_with(None)
        insert.assert_called_once_with(
            email="direct@example.com",
            access_token="token-value",
            totp_secret=None,
            user_id=None,
            user_name=None,
            plan_type=None,
            expires_at=None,
            device_id=None,
            proxy_used=None,
            email_source=None,
            extra={},
            codex_status=None,
            codex_error=None,
            registration_country_code=None,
            registration_country=None,
            registration_region=None,
            registration_ip=None,
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
