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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Resp(response)

    def close(self):
        self.closed = True


class SmsActivateProviderTests(unittest.TestCase):
    def setUp(self):
        sms_provider._CATALOG_CACHE = None
        sms_provider._OFFER_CACHE.clear()

    def test_sms_activate_config_is_exposed_in_webui(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}

        self.assertIn("SMS_API_BASE", fields)
        self.assertEqual(fields["SMS_API_BASE"].get("storage"), "env")
        self.assertIn("SMS_MAX_PRICE", fields)
        self.assertEqual(fields["SMS_MAX_PRICE"].get("storage"), "env")
        self.assertIn("SMS_CANCEL_DELAY", fields)
        self.assertEqual(fields["SMS_PREFERRED_COUNTRIES"]["type"], "list_str_multiline")
        self.assertEqual(fields["SMS_COUNTRY_FAILURE_SWITCH"]["type"], "int")
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

    def test_sms_activate_cancel_waits_for_platform_window(self):
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
            sms_provider._do_cancel_sync("act-1", sms_provider._http)

        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args.args[0], 120)
        self.assertEqual(http.calls[0]["params"]["action"], "setStatus")
        self.assertEqual(http.calls[0]["params"]["status"], "8")

    def test_sms_activate_normal_mode_schedules_non_daemon_cancel(self):
        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch(
            "core.sms_provider.threading.Thread"
        ) as thread:
            sms_provider.cancel("act-1")

        thread.assert_called_once()
        self.assertFalse(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()

    def test_country_catalog_normalizes_sms_activate_response(self):
        http = _Http(
            [
                '{"33":{"eng":"Colombia","rus":"Колумбия","visible":1},'
                '"187":{"eng":"United States","visible":1},'
                '"6":{"eng":"Indonesia","visible":"0"}}'
            ]
        )

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            rows = sms_provider.list_country_catalog(http=http, force=True)

        self.assertEqual(
            rows,
            [
                {"code": "33", "name": "Colombia"},
                {"code": "187", "name": "United States"},
            ],
        )
        self.assertEqual(http.calls[0]["params"]["action"], "getCountries")
        self.assertFalse(http.closed)

    def test_country_offers_filter_requested_countries_and_service(self):
        http = _Http(
            [
                '{"33":{"dr":{"cost":0.11,"count":7}},'
                '"187":{"dr":{"cost":0.19,"count":3}},'
                '"6":{"dr":{"cost":0.08,"count":9}}}'
            ]
        )

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            offers = sms_provider.get_country_offers(
                ["33", "187", "33"], service="dr", http=http, force=True
            )

        self.assertEqual(
            [(x.country_code, str(x.price), x.available_count) for x in offers],
            [
                ("33", "0.11", 7),
                ("187", "0.19", 3),
            ],
        )
        self.assertEqual(http.calls[0]["params"]["action"], "getPrices")
        self.assertEqual(http.calls[0]["params"]["service"], "dr")

    def test_country_offers_ignore_missing_invalid_and_negative_values(self):
        http = _Http(
            [
                '{"1":{"dr":{"cost":0.10}},'
                '"2":{"dr":{"cost":"invalid","count":4}},'
                '"3":{"dr":{"cost":-0.01,"count":2}},'
                '"4":{"dr":{"cost":0.15,"count":-1}},'
                '"5":{"dr":{"cost":0.20,"count":6}},'
                '"6":{"dr":{"cost":0.30,"count":1e9999}}}'
            ]
        )

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            offers = sms_provider.get_country_offers(
                ["1", "2", "3", "4", "5", "6"],
                service="dr",
                http=http,
                force=True,
            )

        self.assertEqual(
            [(x.country_code, str(x.price), x.available_count) for x in offers],
            [("5", "0.2", 6)],
        )

    def test_country_offers_reject_non_sms_activate_provider(self):
        with patch.object(codex_config, "SMS_PROVIDER", "l"):
            with self.assertRaises(sms_provider.SmsProviderError):
                sms_provider.get_country_offers(["33"])

    def test_country_catalog_fresh_cache_and_force_refresh(self):
        http = _Http(
            [
                '{"33":{"eng":"Colombia","visible":1}}',
                '{"187":{"eng":"United States","visible":1}}',
            ]
        )

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            first = sms_provider.list_country_catalog(http=http)
            cached = sms_provider.list_country_catalog(http=http)
            refreshed = sms_provider.list_country_catalog(http=http, force=True)

        self.assertEqual(first, cached)
        self.assertIsNot(first, cached)
        self.assertEqual(refreshed, [{"code": "187", "name": "United States"}])
        self.assertEqual(len(http.calls), 2)

    def test_country_offers_fresh_cache_and_force_refresh(self):
        http = _Http(
            [
                '{"33":{"dr":{"cost":0.11,"count":7}}}',
                '{"33":{"dr":{"cost":0.12,"count":5}}}',
            ]
        )

        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), patch.object(
            codex_config, "SMS_API_BASE", "https://sms.example/handler_api.php"
        ), patch.object(codex_config, "SMS_API_KEY", "secret"):
            first = sms_provider.get_country_offers(["33"], service="dr", http=http)
            cached = sms_provider.get_country_offers(["33"], service="dr", http=http)
            refreshed = sms_provider.get_country_offers(
                ["33"], service="dr", http=http, force=True
            )

        self.assertEqual(first, cached)
        self.assertIsNot(first, cached)
        self.assertEqual(str(refreshed[0].price), "0.12")
        self.assertEqual(refreshed[0].available_count, 5)
        self.assertEqual(len(http.calls), 2)

    def test_country_offers_return_stale_cache_when_refresh_fails(self):
        http = _Http(
            [
                '{"33":{"dr":{"cost":0.11,"count":7}}}',
                RuntimeError("network unavailable"),
            ]
        )

        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config,
            "SMS_API_BASE",
            "https://hero-sms.com/stubs/handler_api.php",
        ), patch.object(codex_config, "SMS_API_KEY", "secret"), self.assertLogs(
            sms_provider.logger, level="WARNING"
        ) as logs:
            first = sms_provider.get_country_offers(["33"], service="dr", http=http)
            stale = sms_provider.get_country_offers(
                ["33"], service="dr", http=http, force=True
            )

        self.assertEqual(first, stale)
        self.assertIsNot(first, stale)
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(any("缓存" in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()
