import unittest
from unittest.mock import Mock, patch

from core.registration_location import lookup_registration_location


class RegistrationLocationTests(unittest.TestCase):
    def test_lookup_uses_exact_proxy_and_normalizes_fields(self):
        transport = Mock(return_value={
            "ip": "203.0.113.10",
            "country_code": "us",
            "country_name": "United States",
            "region": "California",
        })

        result = lookup_registration_location(
            "http://sid:bridge@127.0.0.1:25001",
            transport=transport,
        )

        transport.assert_called_once_with(
            "https://ipapi.co/json/",
            "http://sid:bridge@127.0.0.1:25001",
            5.0,
        )
        self.assertEqual(result, {
            "country_code": "US",
            "country": "United States",
            "region": "California",
            "ip": "203.0.113.10",
        })

    def test_lookup_returns_empty_for_non_proxy_marker(self):
        transport = Mock()
        self.assertEqual(
            lookup_registration_location("browseruse:us", transport=transport),
            {},
        )
        transport.assert_not_called()

    def test_lookup_keeps_partial_response(self):
        transport = Mock(return_value={"country_code": "NL"})
        self.assertEqual(
            lookup_registration_location(
                "socks5://127.0.0.1:7897",
                transport=transport,
            ),
            {"country_code": "NL", "country": "", "region": "", "ip": ""},
        )

    def test_lookup_failure_does_not_escape(self):
        transport = Mock(side_effect=TimeoutError("slow"))
        self.assertEqual(
            lookup_registration_location(
                "http://127.0.0.1:25001",
                transport=transport,
            ),
            {},
        )

    def test_lookup_accepts_country_and_region_name_fallbacks(self):
        transport = Mock(return_value={
            "country": "de",
            "country_name": "Germany",
            "region_name": "Hesse",
        })

        self.assertEqual(
            lookup_registration_location(
                "socks5h://127.0.0.1:7897",
                transport=transport,
            ),
            {
                "country_code": "DE",
                "country": "Germany",
                "region": "Hesse",
                "ip": "",
            },
        )

    def test_lookup_rejects_missing_or_malformed_port(self):
        for proxy_url in (
            "http://127.0.0.1",
            "https://127.0.0.1:not-a-port",
            "socks4://127.0.0.1:70000",
        ):
            with self.subTest(proxy_url=proxy_url):
                transport = Mock()
                self.assertEqual(
                    lookup_registration_location(proxy_url, transport=transport),
                    {},
                )
                transport.assert_not_called()

    @patch("core.registration_location.curl_requests.get")
    def test_default_transport_requests_json_through_exact_proxy(self, get):
        get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"country": "ca"}),
        )
        proxy_url = "https://sid:bridge@127.0.0.1:25001"

        self.assertEqual(
            lookup_registration_location(proxy_url, timeout=1.25),
            {"country_code": "CA", "country": "", "region": "", "ip": ""},
        )

        get.assert_called_once_with(
            "https://ipapi.co/json/",
            headers={"Accept": "application/json"},
            proxy=proxy_url,
            timeout=1.25,
        )

    @patch("core.registration_location.curl_requests.get")
    def test_provider_error_returns_empty_and_logs_without_proxy_credentials(self, get):
        get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"error": True, "reason": "rate limited"}),
        )
        proxy_url = "http://secret-user:secret-password@127.0.0.1:25001"

        with self.assertLogs("core.registration_location", level="WARNING") as logs:
            self.assertEqual(lookup_registration_location(proxy_url), {})

        warning = "\n".join(logs.output)
        self.assertNotIn("secret-user", warning)
        self.assertNotIn("secret-password", warning)

    def test_transport_warning_does_not_log_proxy_credentials(self):
        proxy_url = "http://secret-user:secret-password@127.0.0.1:25001"
        transport = Mock(side_effect=RuntimeError(f"failed via {proxy_url}"))

        with self.assertLogs("core.registration_location", level="WARNING") as logs:
            self.assertEqual(
                lookup_registration_location(proxy_url, transport=transport),
                {},
            )

        warning = "\n".join(logs.output)
        self.assertNotIn("secret-user", warning)
        self.assertNotIn("secret-password", warning)


if __name__ == "__main__":
    unittest.main()
