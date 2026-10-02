import ast
import types
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "core" / "codex_oauth.py"


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Response:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text


class _SmsProviderError(Exception):
    pass


class _SmsNoBalanceError(_SmsProviderError):
    pass


class _SmsProviderConfigurationError(_SmsProviderError):
    pass


class _SmsNoNumbersError(_SmsProviderError):
    pass


class _SmsCodeTimeout(_SmsProviderError):
    pass


class _Http:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _SmsProvider:
    SmsProviderError = _SmsProviderError
    SmsNoBalanceError = _SmsNoBalanceError
    SmsProviderConfigurationError = _SmsProviderConfigurationError
    SmsNoNumbersError = _SmsNoNumbersError
    SmsCodeTimeout = _SmsCodeTimeout

    def __init__(self, mode, send_status=200):
        self.mode = mode
        self.send_status = send_status
        self.http = _Http()
        self.events = []

    def _http(self):
        return self.http

    def preflight_sms_dependency(self, http):
        self.events.append(("preflight",))

    def acquire_number(self, http):
        self.events.append(("acquire",))
        return "activation-1", "15550001111"

    def cancel(self, activation_id, http):
        self.events.append(("cancel", activation_id))

    def complete(self, activation_id, http):
        self.events.append(("complete", activation_id))
        return {"phone_number": "+15550001111", "price_amount": "0.02", "price_currency": "USD"}

    def report_failure(self, activation_id, reason):
        self.events.append(("failure", activation_id, str(reason)))

    def cancel_and_report_failure(self, activation_id, http, reason):
        self.cancel(activation_id, http)
        self.report_failure(activation_id, reason)

    def report_success(self, activation_id):
        self.events.append(("success", activation_id))

    def set_status(self, activation_id, status, http):
        self.events.append(("set_status", activation_id, status))

    def wait_for_sms_code(self, activation_id, http):
        self.events.append(("wait", activation_id))
        if self.mode == "timeout":
            raise _SmsCodeTimeout("code timeout")
        if self.mode == "provider_error":
            raise _SmsProviderError("transport failure")
        return "123456"


def _load_phone_function(provider, responses):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_do_phone_verification"]
    cfg = types.SimpleNamespace(SMS_MAX_RETRIES=1, SMS_CODE_WAIT=1, SMS_POLL_INTERVAL=0)
    response_queue = list(responses)

    def post_json(session, url, payload, **kwargs):
        if provider.mode == "generic_error":
            raise RuntimeError("post failure")
        return response_queue.pop(0)

    namespace = {
        "_cfg": cfg,
        "BrowserSession": object,
        "sms_provider": provider,
        "_sms_provider_name": lambda: "fixture",
        "_sleep_before_phone_retry": lambda *args, **kwargs: None,
        "_post_json": post_json,
        "_response_text": lambda response: response.text,
        "_phone_failure_reason": lambda text, status: "send_failed" if status != 200 else "",
        "_resp_json": lambda response: {"continue_url": "https://callback.test/ok"},
        "logger": _Logger(),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["_do_phone_verification"]


class CodexPhoneFeedbackTests(unittest.TestCase):
    def _run(self, mode, responses):
        provider = _SmsProvider(mode)
        function = _load_phone_function(provider, responses)
        return provider, function(object())

    def test_timeout_releases_before_failure_feedback(self):
        provider = _SmsProvider("timeout")
        function = _load_phone_function(provider, [_Response(200)])
        with self.assertRaises(RuntimeError):
            function(object())

        self.assertEqual(provider.events[-2:], [
            ("cancel", "activation-1"),
            ("failure", "activation-1", "code_timeout"),
        ])

    def test_send_failure_releases_before_failure_feedback(self):
        provider = _SmsProvider("success")
        function = _load_phone_function(provider, [_Response(400, "send limited")])
        with self.assertRaises(RuntimeError):
            function(object())

        self.assertEqual(provider.events[-2:], [
            ("cancel", "activation-1"),
            ("failure", "activation-1", "send_failed"),
        ])

    def test_provider_error_releases_before_failure_feedback(self):
        provider = _SmsProvider("provider_error")
        function = _load_phone_function(provider, [_Response(200)])
        with self.assertRaises(RuntimeError):
            function(object())

        self.assertEqual(provider.events[-2:], [
            ("cancel", "activation-1"),
            ("failure", "activation-1", "transport failure"),
        ])

    def test_generic_post_failure_releases_before_feedback_and_preserves_error(self):
        provider = _SmsProvider("generic_error")
        function = _load_phone_function(provider, [])
        with self.assertRaisesRegex(RuntimeError, "post failure"):
            function(object())

        self.assertEqual(provider.events[-2:], [
            ("cancel", "activation-1"),
            ("failure", "activation-1", "post failure"),
        ])

    def test_success_reports_success_before_complete(self):
        provider = _SmsProvider("success")
        function = _load_phone_function(provider, [_Response(200), _Response(200)])
        result, phone_activation = function(object())

        self.assertEqual(result["continue_url"], "https://callback.test/ok")
        self.assertEqual(phone_activation["phone_number"], "+15550001111")
        self.assertEqual(phone_activation["price_currency"], "USD")
        self.assertLess(provider.events.index(("success", "activation-1")), provider.events.index(("complete", "activation-1")))
        self.assertNotIn(("failure", "activation-1", "code_timeout"), provider.events)


if __name__ == "__main__":
    unittest.main()
