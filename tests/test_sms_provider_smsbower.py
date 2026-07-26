# -*- coding: utf-8 -*-
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
        self.text = body


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        response = self.responses.pop(0)
        return response if isinstance(response, _Resp) else _Resp(response)

    def close(self):
        self.closed = True


def _smsbower_config(**overrides):
    values = {
        "SMS_PROVIDER": "smsbower",
        "SMS_SERVICE": "dr",
        "SMS_COUNTRY": "187",
        "SMS_MAX_PRICE": "0.20",
        "SMSBOWER_API_BASE": "https://smsbower.page/stubs/handler_api.php",
        "SMSBOWER_API_KEY": "smsbower-key",
    }
    values.update(overrides)
    stack = ExitStack()
    for key, value in values.items():
        stack.enter_context(patch.object(codex_config, key, value))
    return stack


class SmsBowerProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields_include_smsbower(self):
        self.assertIn("SMSBOWER_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertTrue(fields["SMSBOWER_API_KEY"].get("secret"))
        self.assertIn("SMSBOWER_API_BASE", fields)
        self.assertIn("SMS_MAX_PRICE", fields)
        provider_values = {item["value"] for item in fields["SMS_PROVIDER"]["options"]}
        self.assertIn("smsbower", provider_values)

    def test_acquire_number_uses_smsbower_api_and_common_filters(self):
        http = _Http(["ACCESS_NUMBER:sb-1:+12025550123"])
        with _smsbower_config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "sb-1")
        self.assertEqual(phone, "12025550123")
        self.assertEqual(http.calls[0]["url"], "https://smsbower.page/stubs/handler_api.php")
        self.assertEqual(http.calls[0]["params"], {
            "api_key": "smsbower-key",
            "action": "getNumber",
            "service": "dr",
            "country": "187",
            "maxPrice": "0.20",
        })

    def test_wait_for_code_and_set_status_use_smsbower_api(self):
        http = _Http(["STATUS_OK:654321", "ACCESS_ACTIVATION"])
        with _smsbower_config():
            code = sms_provider.wait_for_sms_code("sb-2", http=http, max_wait=1)
            result = sms_provider.set_status("sb-2", 6, http=http)

        self.assertEqual(code, "654321")
        self.assertEqual(result, "ACCESS_ACTIVATION")
        self.assertEqual(http.calls[0]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[1]["params"]["action"], "setStatus")
        self.assertEqual(http.calls[1]["params"]["status"], "6")
        self.assertTrue(all(call["params"]["api_key"] == "smsbower-key" for call in http.calls))

    def test_early_cancel_error_is_explained(self):
        http = _Http(["EARLY_CANCEL_DENIED"])
        with _smsbower_config():
            with self.assertRaisesRegex(sms_provider.SmsProviderError, "两分钟内不能取消"):
                sms_provider.set_status("sb-3", 8, http=http)


if __name__ == "__main__":
    unittest.main()
