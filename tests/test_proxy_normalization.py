# -*- coding: utf-8 -*-
import unittest

from config.proxy import normalize_proxy_list, normalize_proxy_url


class ProxyNormalizationTests(unittest.TestCase):
    def test_host_port_gets_default_scheme(self):
        self.assertEqual(
            normalize_proxy_url("127.0.0.1:7897"),
            "http://127.0.0.1:7897",
        )

    def test_host_port_username_password_format_encodes_credentials(self):
        self.assertEqual(
            normalize_proxy_url("proxy.example.test:8080:user:p@ ss"),
            "http://user:p%40%20ss@proxy.example.test:8080",
        )

    def test_username_password_host_port_format_encodes_credentials(self):
        self.assertEqual(
            normalize_proxy_url("user:p@ ss:proxy.example.test:8080"),
            "http://user:p%40%20ss@proxy.example.test:8080",
        )

    def test_existing_scheme_is_preserved(self):
        self.assertEqual(
            normalize_proxy_url("socks5h://127.0.0.1:7897"),
            "socks5h://127.0.0.1:7897",
        )

    def test_invalid_port_is_preserved(self):
        self.assertEqual(
            normalize_proxy_url("proxy.example.test:not-a-port"),
            "proxy.example.test:not-a-port",
        )

    def test_list_filters_blank_entries(self):
        self.assertEqual(
            normalize_proxy_list(["", "  ", "127.0.0.1:7897"]),
            ["http://127.0.0.1:7897"],
        )


if __name__ == "__main__":
    unittest.main()
