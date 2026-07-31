# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import proxy as proxy_config
from core import roxybrowser_client as client


class _FakeServer:
    server_address = ("127.0.0.1", 25001)


class ProxyBridgeConfigTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            "enabled": proxy_config.PROXY_CHAIN_ENABLED,
            "host": proxy_config.PROXY_CHAIN_LISTEN_HOST,
            "port": proxy_config.PROXY_CHAIN_LISTEN_PORT,
            "preproxy": proxy_config.PROXY_CHAIN_PREPROXY,
            "upstream": proxy_config.PROXY_CHAIN_UPSTREAM,
            "server": client._PROXY_CHAIN_SERVER,
            "template": client._PROXY_CHAIN_TEMPLATE,
        }
        proxy_config.PROXY_CHAIN_ENABLED = True
        proxy_config.PROXY_CHAIN_LISTEN_HOST = "127.0.0.1"
        proxy_config.PROXY_CHAIN_LISTEN_PORT = 25001
        proxy_config.PROXY_CHAIN_PREPROXY = "http://127.0.0.1:7897"
        proxy_config.PROXY_CHAIN_UPSTREAM = "http://user-region-KR-sid-{sid}-t-3:pass@upstream.example:3000"
        client._PROXY_CHAIN_SERVER = None
        client._PROXY_CHAIN_TEMPLATE = ""

    def tearDown(self):
        proxy_config.PROXY_CHAIN_ENABLED = self.old["enabled"]
        proxy_config.PROXY_CHAIN_LISTEN_HOST = self.old["host"]
        proxy_config.PROXY_CHAIN_LISTEN_PORT = self.old["port"]
        proxy_config.PROXY_CHAIN_PREPROXY = self.old["preproxy"]
        proxy_config.PROXY_CHAIN_UPSTREAM = self.old["upstream"]
        client._PROXY_CHAIN_SERVER = self.old["server"]
        client._PROXY_CHAIN_TEMPLATE = self.old["template"]

    @patch("tools.proxy_chain_bridge.start_bridge", return_value=_FakeServer())
    def test_prepare_proxy_uses_local_bridge_and_sid(self, start):
        result = client.prepare_proxy_for_roxy(
            "http://user-region-KR-sid-abc123-t-3:pass@upstream.example:3000"
        )
        self.assertIn("sid-abc123", result)
        self.assertIn("127.0.0.1:25001", result)
        start.assert_called_once()

    @patch("tools.proxy_chain_bridge.start_bridge", return_value=_FakeServer())
    def test_prepare_proxy_starts_bridge_once(self, start):
        first = client.prepare_proxy_for_roxy(
            "http://user-region-KR-sid-abc123-t-3:pass@upstream.example:3000"
        )
        second = client.prepare_proxy_for_roxy(
            "http://user-region-KR-sid-def456-t-3:pass@upstream.example:3000"
        )
        self.assertIn("sid-abc123", first)
        self.assertIn("sid-def456", second)
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
