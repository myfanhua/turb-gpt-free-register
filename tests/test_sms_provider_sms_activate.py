# -*- coding: utf-8 -*-
import unittest
import time
from unittest.mock import patch

from config import codex as codex_config
from core import sms_provider
from webui import config_editor


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


class SmsActivateProviderTests(unittest.TestCase):
    def test_sms_activate_config_is_exposed_in_webui(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}

        self.assertIn("SMS_API_BASE", fields)
        self.assertEqual(fields["SMS_API_BASE"].get("storage"), "env")
        self.assertIn("SMS_MAX_PRICE", fields)
        self.assertEqual(fields["SMS_MAX_PRICE"].get("storage"), "env")
        self.assertIn("SMS_CANCEL_DELAY", fields)
        self.assertTrue(hasattr(codex_config, "SMS_API_BASE"))
        self.assertTrue(hasattr(codex_config, "SMS_CANCEL_DELAY"))

    def test_sms_activate_provider_aliases_are_normalized(self):
        for value in ("sms_activate", "sms-activate", "smsactivate", "hero_sms", "hero-sms"):
            with self.subTest(value=value), patch.object(codex_config, "SMS_PROVIDER", value):
                self.assertEqual(sms_provider._provider(), "sms_activate")

    def test_sms_activate_acquire_uses_configured_handler(self):
        http = _Http(["ACCESS_NUMBER:act-1:15551234567"])

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"), patch.object(
            codex_config, "SMS_SERVICE", "dr"
        ), patch.object(codex_config, "SMS_COUNTRY", "12"), patch.object(
            codex_config, "SMS_MAX_PRICE", "1.5"
        ):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("act-1", "15551234567"))
        self.assertEqual(
            http.calls[0]["url"],
            "https://hero-sms.com/stubs/handler_api.php",
        )
        self.assertEqual(http.calls[0]["params"]["action"], "getNumber")
        self.assertEqual(http.calls[0]["params"]["api_key"], "secret")
        self.assertEqual(http.calls[0]["params"]["service"], "dr")
        self.assertEqual(http.calls[0]["params"]["country"], "12")
        self.assertEqual(http.calls[0]["params"]["maxPrice"], "1.5")

    def test_sms_activate_wait_for_code_parses_status_ok(self):
        http = _Http(["STATUS_OK:482913"])

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            code = sms_provider.wait_for_sms_code(
                "act-1",
                http=http,
                max_wait=1,
                poll_interval=0,
            )

        self.assertEqual(code, "482913")
        self.assertEqual(http.calls[0]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[0]["params"]["id"], "act-1")

    def test_sms_activate_complete_sends_status_six(self):
        http = _Http(["ACCESS_ACTIVATION"])

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            sms_provider.complete("act-1", http=http)

        self.assertEqual(http.calls[0]["params"]["action"], "setStatus")
        self.assertEqual(http.calls[0]["params"]["status"], "6")

    def test_sms_activate_cancel_has_no_grizzly_delay(self):
        http = _Http(["ACCESS_CANCEL"])
        sms_provider._ACQUIRED_AT["act-1"] = time.time()

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config, "SMS_CANCEL_DELAY", -1
        ), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"), patch.object(
            sms_provider, "_http", return_value=http
        ), patch("core.sms_provider.time.sleep") as sleep:
            sms_provider.cancel("act-1", background=False)

        sleep.assert_not_called()
        self.assertEqual(http.calls[0]["params"]["action"], "setStatus")
        self.assertEqual(http.calls[0]["params"]["status"], "8")


if __name__ == "__main__":
    unittest.main()
