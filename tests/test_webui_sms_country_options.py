# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import sms_provider
from webui.app import create_app


class WebUiSmsCountryOptionsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    @patch("core.sms_provider.list_country_catalog")
    def test_authenticated_route_normalizes_catalog_and_filters_blank_codes(self, catalog):
        catalog.return_value = [
            {"code": 0, "name": "Russia"},
            {"code": 33, "name": " Colombia "},
            {"code": "  ", "name": "Blank"},
            {"code": "187", "name": "United States"},
            {"code": None, "name": "Missing"},
        ]

        response = self.client.get(
            "/api/sms/countries",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "ok": True,
            "countries": [
                {"code": "0", "name": "Russia"},
                {"code": "33", "name": "Colombia"},
                {"code": "187", "name": "United States"},
            ],
        })
        catalog.assert_called_once_with(force=False)

    @patch("config.codex.SMS_API_KEY", "catalog-secret-key")
    @patch("config.codex.SMS_API_BASE", "https://sms.example/private-handler")
    @patch("core.sms_provider.list_country_catalog")
    def test_provider_error_returns_502_without_leaking_sms_configuration(self, catalog):
        catalog.side_effect = sms_provider.SmsProviderError(
            "request to https://sms.example/private-handler failed with api_key=catalog-secret-key"
        )

        with self.assertLogs("webui.app", level="WARNING") as captured:
            response = self.client.get(
                "/api/sms/countries",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 502)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"].startswith("SmsProviderError: "))
        combined = payload["error"] + "\n" + "\n".join(captured.output)
        self.assertNotIn("catalog-secret-key", combined)
        self.assertNotIn("https://sms.example/private-handler", combined)

    @patch("core.sms_provider.list_country_catalog")
    def test_whitespace_only_country_name_falls_back_to_normalized_code(self, catalog):
        catalog.return_value = [{"code": "33", "name": "   "}]

        response = self.client.get(
            "/api/sms/countries",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["countries"], [
            {"code": "33", "name": "33"},
        ])

    @patch("core.sms_provider.list_country_catalog")
    def test_unauthenticated_route_matches_other_api_auth_behavior(self, catalog):
        response = self.client.get("/api/sms/countries")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["ok"], False)
        self.assertIn("未授权", response.get_json()["error"])
        catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
