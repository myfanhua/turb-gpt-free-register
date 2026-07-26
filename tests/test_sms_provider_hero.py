# -*- coding: utf-8 -*-
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import sms_provider
from webui import config_editor


class _Resp:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        response = self.responses.pop(0)
        if isinstance(response, _Resp):
            return response
        return _Resp(response)

    def close(self):
        self.closed = True


def _hero_config(**overrides):
    values = {
        "SMS_PROVIDER": "hero",
        "SMS_SERVICE": "dr",
        "SMS_COUNTRY": "187",
        "HERO_SMS_API_BASE": "https://hero-sms.com/stubs/handler_api.php",
        "HERO_SMS_API_KEY": "hero-key",
        "HERO_SMS_PRICE_MODE": "any",
        "HERO_SMS_MIN_PRICE": "",
        "HERO_SMS_MAX_PRICE": "",
        "HERO_SMS_RANGE_STRATEGY": "lowest",
        "HERO_SMS_OPERATOR": "",
    }
    values.update(overrides)
    stack = ExitStack()
    for key, value in values.items():
        stack.enter_context(patch.object(codex_config, key, value))
    return stack


class HeroSmsProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields_include_hero(self):
        self.assertIn("HERO_SMS_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertTrue(fields["HERO_SMS_API_KEY"].get("secret"))
        self.assertIn("HERO_SMS_PRICE_MODE", fields)
        provider_values = {item["value"] for item in fields["SMS_PROVIDER"]["options"]}
        self.assertIn("hero", provider_values)

    def test_acquire_number_uses_fixed_price(self):
        http = _Http([{
            "activationId": "hero-1",
            "phoneNumber": "+12025550123",
            "activationCost": 0.08,
        }])
        with _hero_config(
            HERO_SMS_PRICE_MODE="fixed",
            HERO_SMS_MAX_PRICE="0.0800",
            HERO_SMS_OPERATOR="tmobile,verizon",
        ):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "hero-1")
        self.assertEqual(phone, "12025550123")
        params = http.calls[0]["params"]
        self.assertEqual(params["action"], "getNumberV2")
        self.assertEqual(params["api_key"], "hero-key")
        self.assertEqual(params["maxPrice"], "0.08")
        self.assertEqual(params["fixedPrice"], "true")
        self.assertEqual(params["operator"], "tmobile,verizon")

    def test_openai_display_name_is_normalized_to_hero_service_code(self):
        http = _Http([{
            "activationId": "hero-alias",
            "phoneNumber": "+12025550126",
            "activationCost": 0.08,
        }])
        with _hero_config(SMS_SERVICE="openai"):
            sms_provider.acquire_number(http=http)

        self.assertEqual(http.calls[0]["params"]["service"], "dr")

    def test_range_mode_selects_lowest_available_price(self):
        http = _Http([
            {
                "0": {
                    "country": 187,
                    "freePriceMap": {"0.03": 2, "0.06": 5, "0.09": 1, "0.12": 8},
                },
            },
            {
                "activationId": "hero-2",
                "phoneNumber": "12025550124",
                "activationCost": 0.06,
            },
        ])
        with _hero_config(
            HERO_SMS_PRICE_MODE="range",
            HERO_SMS_MIN_PRICE="0.05",
            HERO_SMS_MAX_PRICE="0.10",
            HERO_SMS_RANGE_STRATEGY="lowest",
        ):
            activation_id, _ = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "hero-2")
        self.assertEqual(http.calls[0]["params"]["action"], "getTopCountriesByServiceRank")
        self.assertEqual(http.calls[0]["params"]["freePrice"], "true")
        purchase = http.calls[1]["params"]
        self.assertEqual(purchase["maxPrice"], "0.06")
        self.assertEqual(purchase["fixedPrice"], "true")

    def test_range_mode_can_select_highest_available_price(self):
        http = _Http([
            {"dr": [{"country": "187", "physicalPriceMap": {"0.06": 5, "0.09": 1}}]},
            {"activationId": "hero-3", "phoneNumber": "12025550125", "activationCost": 0.09},
        ])
        with _hero_config(
            HERO_SMS_PRICE_MODE="range",
            HERO_SMS_MIN_PRICE="0.05",
            HERO_SMS_MAX_PRICE="0.10",
            HERO_SMS_RANGE_STRATEGY="highest",
        ):
            sms_provider.acquire_number(http=http)

        self.assertEqual(http.calls[1]["params"]["maxPrice"], "0.09")

    def test_range_mode_reports_available_prices_when_no_tier_matches(self):
        http = _Http([{
            "dr": [{"country": 187, "physicalPriceMap": {"0.03": 2, "0.12": 8}}],
        }])
        with _hero_config(
            HERO_SMS_PRICE_MODE="range",
            HERO_SMS_MIN_PRICE="0.05",
            HERO_SMS_MAX_PRICE="0.10",
        ):
            with self.assertRaisesRegex(sms_provider.SmsNoNumbersError, "0.03, 0.12"):
                sms_provider.acquire_number(http=http)

    def test_wait_for_code_and_set_status_use_hero_api(self):
        http = _Http(["STATUS_OK:654321", "ACCESS_ACTIVATION"])
        with _hero_config():
            code = sms_provider.wait_for_sms_code("hero-4", http=http, max_wait=1)
            result = sms_provider.set_status("hero-4", 6, http=http)

        self.assertEqual(code, "654321")
        self.assertEqual(result, "ACCESS_ACTIVATION")
        self.assertEqual(http.calls[0]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[1]["params"]["action"], "setStatus")
        self.assertEqual(http.calls[1]["params"]["status"], "6")

    def test_http_401_is_reported_as_invalid_api_key(self):
        http = _Http([_Resp("unauthorized", status_code=401)])
        with _hero_config():
            with self.assertRaisesRegex(sms_provider.SmsProviderError, "API Key 无效"):
                sms_provider.acquire_number(http=http)


if __name__ == "__main__":
    unittest.main()
