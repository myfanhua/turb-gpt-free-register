# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import chatgpt_plan
from core.session import BrowserSession


class _Response:
    status_code = 200
    text = '{"accounts":{"default":{"account":{"account_id":"acc-1","plan_type":"free"},"entitlement":{},"eligible_promo_campaigns":{}}}}'
    headers = {}

    def json(self):
        return {
            "accounts": {
                "default": {
                    "account": {"account_id": "acc-1", "plan_type": "free"},
                    "entitlement": {},
                    "eligible_promo_campaigns": {},
                }
            }
        }


class _FakeBrowserSession:
    proxies_seen = []

    def __init__(self, proxy=None, **_kwargs):
        self.proxy = proxy
        self.device_id = "device-1"
        self.session = self
        self.proxies_seen.append(proxy)

    def _get_common_headers(self):
        return {}

    def navigator_language(self):
        return "en-US"

    def get(self, *_args, **_kwargs):
        if self.proxy:
            raise RuntimeError("ProxyError: SOCKS proxy closed connection")
        return _Response()

    def close(self):
        return None


class AccountCheckNetworkTests(unittest.TestCase):
    def test_explicit_direct_session_ignores_ambient_proxy_environment(self):
        env = BrowserSession(proxy="", detect_exit_geo=False, fingerprint_seed="direct-check")
        try:
            self.assertFalse(env.session.trust_env)
            self.assertEqual(env.session.proxies, {})
        finally:
            env.session.close()

    def test_auto_plan_route_falls_back_to_true_direct_on_proxy_transport_failure(self):
        _FakeBrowserSession.proxies_seen = []

        def route(explicit_proxy=None):
            if explicit_proxy == "":
                return {
                    "proxy": "",
                    "proxy_mode": "request",
                    "network_route": "direct",
                    "proxy_used": None,
                    "proxy_fallback_reason": None,
                }
            return {
                "proxy": "socks5h://user:pass@proxy.example:3000",
                "proxy_mode": "auto",
                "network_route": "proxy",
                "proxy_used": "socks5h://***:***@proxy.example:3000",
                "proxy_fallback_reason": None,
            }

        with patch.object(chatgpt_plan, "BrowserSession", _FakeBrowserSession), patch.object(
            chatgpt_plan, "resolve_plan_check_route", side_effect=route
        ):
            result = chatgpt_plan.check_account_plan(
                "stored-access-token",
                proxy=None,
                timeout=1,
                max_attempts=3,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["current_plan_type"], "free")
        self.assertEqual(result["network_route"], "direct_fallback")
        self.assertEqual(result["proxy_mode"], "auto")
        self.assertEqual(
            result["proxy_used"],
            "socks5h://***:***@proxy.example:3000",
        )
        self.assertIn("代理传输失败", result["proxy_fallback_reason"])
        self.assertEqual(
            _FakeBrowserSession.proxies_seen,
            ["socks5h://user:pass@proxy.example:3000", ""],
        )

    def test_explicit_proxy_failure_does_not_override_requested_route(self):
        _FakeBrowserSession.proxies_seen = []
        with patch.object(chatgpt_plan, "BrowserSession", _FakeBrowserSession), patch.object(
            chatgpt_plan,
            "resolve_plan_check_route",
            return_value={
                "proxy": "socks5h://user:pass@proxy.example:3000",
                "proxy_mode": "request",
                "network_route": "proxy",
                "proxy_used": "socks5h://***:***@proxy.example:3000",
                "proxy_fallback_reason": None,
            },
        ):
            result = chatgpt_plan.check_account_plan(
                "stored-access-token",
                proxy="socks5h://user:pass@proxy.example:3000",
                timeout=1,
                max_attempts=1,
                retry_delay=0,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["network_route"], "proxy")
        self.assertEqual(_FakeBrowserSession.proxies_seen, ["socks5h://user:pass@proxy.example:3000"])


if __name__ == "__main__":
    unittest.main()
