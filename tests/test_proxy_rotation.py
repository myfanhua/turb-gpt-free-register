# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import proxy


class ProxyRotationTests(unittest.TestCase):
    def test_pick_proxy_materializes_fresh_sid_for_each_registration(self):
        template = (
            "socks5h://user-region-KR-sid-{sid}-t-3:password@"
            "us.1024proxy.io:3000"
        )
        with patch.object(proxy, "PROXY_CHAIN_ENABLED", False), patch.object(
            proxy, "PROXY_POOL", [template]
        ), patch(
            "config.proxy.secrets.token_hex", side_effect=["aaa111", "bbb222"]
        ):
            first = proxy.pick_proxy()
            second = proxy.pick_proxy()

        self.assertIn("region-KR-sid-aaa111-t-3", first)
        self.assertIn("region-KR-sid-bbb222-t-3", second)
        self.assertNotIn("{sid}", first)
        self.assertNotEqual(first, second)

    def test_pick_proxy_keeps_static_proxy_unchanged(self):
        static = "socks5h://127.0.0.1:7897"
        with patch.object(proxy, "PROXY_CHAIN_ENABLED", False), \
             patch.object(proxy, "PROXY_POOL", [static]):
            self.assertEqual(proxy.pick_proxy(), static)


if __name__ == "__main__":
    unittest.main()
