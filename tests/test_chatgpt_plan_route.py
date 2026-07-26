# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.chatgpt_plan import resolve_plan_check_route


class PlanCheckRouteTests(unittest.TestCase):
    def test_auto_falls_back_to_pool_when_dedicated_proxy_is_a_token(self):
        with (
            patch("config.proxy.PLAN_CHECK_PROXY_MODE", "auto"),
            patch("config.proxy.PLAN_CHECK_PROXY", "token-without-a-port"),
            patch("config.proxy.pick_proxy", return_value="socks5h://user:pass@proxy.example:1080"),
        ):
            route = resolve_plan_check_route()

        self.assertEqual(route["proxy"], "socks5h://user:pass@proxy.example:1080")
        self.assertEqual(route["network_route"], "proxy")
        self.assertIn("PLAN_CHECK_PROXY", route["proxy_fallback_reason"])

    def test_auto_falls_back_to_direct_when_all_proxy_values_are_invalid(self):
        with (
            patch("config.proxy.PLAN_CHECK_PROXY_MODE", "auto"),
            patch("config.proxy.PLAN_CHECK_PROXY", "token-without-a-port"),
            patch("config.proxy.pick_proxy", return_value="another-token"),
        ):
            route = resolve_plan_check_route()

        self.assertEqual(route["proxy"], "")
        self.assertEqual(route["network_route"], "direct")
        self.assertIn("PROXY_POOL", route["proxy_fallback_reason"])

    def test_proxy_mode_reports_invalid_configuration(self):
        with (
            patch("config.proxy.PLAN_CHECK_PROXY_MODE", "proxy"),
            patch("config.proxy.PLAN_CHECK_PROXY", "token-without-a-port"),
            patch("config.proxy.pick_proxy", return_value=""),
        ):
            with self.assertRaisesRegex(ValueError, "PLAN_CHECK_PROXY"):
                resolve_plan_check_route()

    def test_explicit_proxy_requires_host_and_port(self):
        with self.assertRaisesRegex(ValueError, "代理格式无效"):
            resolve_plan_check_route("token-without-a-port")


if __name__ == "__main__":
    unittest.main()
