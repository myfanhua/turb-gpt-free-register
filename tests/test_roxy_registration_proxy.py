import unittest
from unittest.mock import patch

from config import roxybrowser as roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient


class RoxyRegistrationProxyTests(unittest.TestCase):
    def test_open_profile_exposes_actual_created_proxy(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        upstream = "http://user-region-KR-sid-abc123-t-3:pass@upstream.example:3000"
        bridge = "http://sid-abc123:bridge@127.0.0.1:25001"
        responses = [
            {"code": 0, "data": {"dirId": "profile-1"}},
            {"code": 0, "data": {"http": "127.0.0.1:9222"}},
        ]
        with patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True), \
             patch.object(roxy_cfg, "ROXY_PROFILE_ID", ""), \
             patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "1"), \
             patch.object(roxy_cfg, "ROXY_CREATE_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy", return_value=upstream), \
             patch("core.roxybrowser_client.prepare_proxy_for_roxy", return_value=bridge), \
             patch.object(client, "request", side_effect=responses):
            opened = client.open_profile()

        self.assertEqual(opened.profile_id, "profile-1")
        self.assertEqual(opened.registration_proxy, bridge)

    def test_configured_profile_keeps_registration_proxy_empty(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        response = {"code": 0, "data": {"http": "127.0.0.1:9222"}}
        with patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False), \
             patch.object(roxy_cfg, "ROXY_PROFILE_ID", "profile-existing"), \
             patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "1"), \
             patch.object(client, "request", return_value=response):
            opened = client.open_profile()

        self.assertIsNone(opened.registration_proxy)

    def test_explicit_proxy_overrides_pool_and_is_exposed_on_open_result(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        upstream = "http://user-region-US-sid-fixed01:pass@upstream.example:3000"
        bridge = "http://sid-fixed01:bridge@127.0.0.1:25001"
        responses = [
            {"code": 0, "data": {"dirId": "profile-explicit"}},
            {"code": 0, "data": {"http": "127.0.0.1:9222"}},
        ]
        with patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True), \
             patch.object(roxy_cfg, "ROXY_PROFILE_ID", ""), \
             patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "1"), \
             patch.object(roxy_cfg, "ROXY_CREATE_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy") as pick_proxy, \
             patch("core.roxybrowser_client.prepare_proxy_for_roxy", return_value=bridge) as prepare, \
             patch.object(client, "request", side_effect=responses) as request:
            opened = client.open_profile(proxy_url=upstream)

        pick_proxy.assert_not_called()
        prepare.assert_called_once_with(upstream)
        create_body = request.call_args_list[0].kwargs["json_body"]
        self.assertEqual(create_body["proxyInfo"]["host"], "127.0.0.1")
        self.assertEqual(create_body["proxyInfo"]["port"], "25001")
        self.assertEqual(opened.registration_proxy, bridge)


if __name__ == "__main__":
    unittest.main()
