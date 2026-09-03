# -*- coding: utf-8 -*-
import json
import unittest

from core.trial_payment_probe import parse_payment_method_types, probe_trial_payment_methods


class _Response:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self):
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url.endswith("/backend-api/payments/checkout"):
            return _Response(payload={"checkout_session_id": "cs_live_SECRETID"})
        if "/v1/payment_pages/" in url and url.endswith("/init"):
            return _Response(payload={"payment_method_types": ["card", "paypal", "momo"]})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        params = kwargs.get("params") or {}
        requested = params.get("deferred_intent[payment_method_types][0]")
        if requested == "momo":
            return _Response(payload={"payment_method_specs": [{"type": "momo"}]})
        if requested == "paypal":
            return _Response(payload={"payment_method_specs": [{"type": "paypal"}]})
        if requested == "gopay":
            return _Response(payload={"payment_method_specs": []})
        raise AssertionError(f"unexpected GET params {params}")

    def close(self):
        self.closed = True


class TrialPaymentProbeTests(unittest.TestCase):
    def test_parse_payment_method_types_accepts_top_level_and_specs(self):
        self.assertEqual(
            parse_payment_method_types({"payment_method_types": ["card", "paypal", "card"]}),
            ["card", "paypal"],
        )
        self.assertEqual(
            parse_payment_method_types({"payment_method_specs": [{"type": "momo"}, {"type": "gopay"}]}),
            ["momo", "gopay"],
        )

    def test_probe_creates_checkout_preview_and_reports_each_method_without_confirm(self):
        session = _Session()
        result = probe_trial_payment_methods(
            "Bearer fixture.token.value",
            country="ID",
            session_factory=lambda **kwargs: session,
            stripe_publishable_keys=["pk_live_fixture_1234567890"],
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["checkout_session_created"])
        self.assertEqual(
            {item["id"]: item["status"] for item in result["methods"]},
            {"momo": "supported", "paypal": "supported", "gopay": "unsupported"},
        )
        self.assertNotIn("checkout_session_id", result)
        self.assertFalse(any(call[1].endswith("/confirm") for call in session.calls))
        self.assertTrue(session.closed)

    def test_probe_failure_redacts_checkout_identifiers_and_raw_response(self):
        class FailedSession(_Session):
            def post(self, url, **kwargs):
                self.calls.append(("post", url, kwargs))
                return _Response(status_code=403, payload={"id": "cs_live_SECRETID", "client_secret": "secret-value"}, text="secret-value cs_live_SECRETID")

        session = FailedSession()
        result = probe_trial_payment_methods(
            "fixture.token.value",
            session_factory=lambda **kwargs: session,
            stripe_publishable_keys=["pk_live_fixture_1234567890"],
        )
        rendered = repr(result)
        self.assertFalse(result["ok"])
        self.assertNotIn("cs_live_SECRETID", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("pk_live_fixture_1234567890", rendered)
        self.assertTrue(session.closed)

    def test_probe_reads_openai_custom_checkout_methods_without_confirm(self):
        class CustomCheckoutSession(_Session):
            def post(self, url, **kwargs):
                self.calls.append(("post", url, kwargs))
                if url.endswith("/backend-api/payments/checkout"):
                    return _Response(payload={
                        "checkout_session_id": "oaics_CUSTOM_SESSION",
                        "processor_entity": "openai_ie",
                    })
                raise AssertionError(f"unexpected POST {url}")

            def get(self, url, **kwargs):
                self.calls.append(("get", url, kwargs))
                if "/backend-api/payments/checkout/openai_ie/oaics_CUSTOM_SESSION" in url:
                    return _Response(payload={
                        "custom_payment_methods": [
                            {"id": "cpmt_MOMO_SECRET", "payment_method_type": "momo"},
                            {"id": "cpmt_PAYPAL_SECRET", "label": "PayPal"},
                        ]
                    })
                raise AssertionError(f"unexpected GET {url}")

        session = CustomCheckoutSession()
        result = probe_trial_payment_methods(
            "fixture.token.value",
            country="DE",
            session_factory=lambda **kwargs: session,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["checkout_session_created"])
        self.assertEqual(
            {item["id"]: item["status"] for item in result["methods"]},
            {"momo": "supported", "paypal": "supported", "gopay": "unsupported"},
        )
        rendered = repr(result)
        self.assertNotIn("oaics_CUSTOM_SESSION", rendered)
        self.assertNotIn("cpmt_MOMO_SECRET", rendered)
        self.assertFalse(any(call[1].endswith("/confirm") for call in session.calls))
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
