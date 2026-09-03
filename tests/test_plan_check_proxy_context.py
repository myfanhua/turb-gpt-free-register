# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db, plan_check_service


class PlanCheckProxyContextTests(unittest.TestCase):
    def test_latest_successful_registration_job_provides_account_proxy_context(self):
        jobs = [
            {
                "id": 34,
                "status": "success",
                "account_id": 7,
                "email": "sprain.islands.9n@icloud.com",
                "proxy_provider": "cliproxy_traffic",
                "proxy_country": "JP",
                "completed_at": "2026-08-29T23:54:40",
            },
            {
                "id": 35,
                "status": "success",
                "account_id": 8,
                "email": "spud_pottery8w@icloud.com",
                "proxy_provider": "cliproxy_traffic",
                "proxy_country": "JP",
                "completed_at": "2026-08-29T23:54:53",
            },
        ]
        with patch.object(db, "_load_jobs", return_value=jobs):
            context = db.get_account_proxy_context(
                account_id=8,
                email="spud_pottery8w@icloud.com",
            )

        self.assertEqual(context["proxy_provider"], "cliproxy_traffic")
        self.assertEqual(context["proxy_country"], "JP")
        self.assertEqual(context["job_id"], 35)

    def test_running_registration_job_is_opt_in_for_auto_check(self):
        jobs = [
            {
                "id": 36,
                "status": "running",
                "email": "running@example.test",
                "proxy_provider": "cliproxy_traffic",
                "proxy_country": "JP",
                "started_at": "2026-08-30T10:20:00",
            },
        ]
        with patch.object(db, "_load_jobs", return_value=jobs):
            self.assertEqual(
                db.get_account_proxy_context(email="running@example.test"),
                {},
            )
            context = db.get_account_proxy_context(
                email="running@example.test",
                include_running=True,
            )

        self.assertEqual(context["proxy_provider"], "cliproxy_traffic")
        self.assertEqual(context["proxy_country"], "JP")
        self.assertEqual(context["job_id"], 36)

    def test_plan_check_uses_registration_country_when_proxy_is_omitted(self):
        context = {
            "proxy_provider": "cliproxy_traffic",
            "proxy_country": "JP",
            "job_id": 35,
        }
        with patch.object(db, "get_account_proxy_context", return_value=context), patch(
            "core.proxy_provider.build_proxy", return_value="socks5h://masked@example:3010"
        ) as build_proxy:
            proxy, selected = plan_check_service.resolve_account_plan_proxy(
                account_id=8,
                email="spud_pottery8w@icloud.com",
                proxy=None,
            )

        self.assertEqual(proxy, "socks5h://masked@example:3010")
        self.assertEqual(selected, context)
        build_proxy.assert_called_once_with("cliproxy_traffic", "JP", job_id=35)

    def test_registration_auto_can_use_running_job_context(self):
        context = {
            "proxy_provider": "cliproxy_traffic",
            "proxy_country": "JP",
            "job_id": 35,
        }
        with patch.object(db, "get_account_proxy_context", return_value=context) as get_context, patch(
            "core.proxy_provider.build_proxy", return_value="socks5h://masked@example:3010"
        ):
            proxy, selected = plan_check_service.resolve_account_plan_proxy(
                account_id=8,
                email="spud_pottery8w@icloud.com",
                proxy=None,
                trigger="registration_auto",
            )

        self.assertEqual(proxy, "socks5h://masked@example:3010")
        self.assertEqual(selected, context)
        get_context.assert_called_once_with(
            account_id=8,
            email="spud_pottery8w@icloud.com",
            include_running=True,
        )

    def test_explicit_proxy_stays_authoritative(self):
        with patch.object(db, "get_account_proxy_context") as get_context, patch(
            "core.proxy_provider.build_proxy"
        ) as build_proxy:
            proxy, selected = plan_check_service.resolve_account_plan_proxy(
                account_id=8,
                email="spud_pottery8w@icloud.com",
                proxy="",
            )

        self.assertEqual(proxy, "")
        self.assertIsNone(selected)
        get_context.assert_not_called()
        build_proxy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
