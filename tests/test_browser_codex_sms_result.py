import ast
import contextvars
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1] / "core"


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _SmsProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _SmsProvider:
    SmsProviderError = _SmsProviderError

    @staticmethod
    def classify_sms_exception(reason):
        if reason.code == "sms_no_balance":
            return {
                "status": "blocked",
                "error_code": "sms_no_balance",
                "retryable": False,
                "message": "SMS provider has no balance",
            }
        return {
            "status": "failed",
            "error_code": "sms_no_numbers",
            "retryable": True,
            "message": "SMS provider has no numbers",
        }


def _codex_result(*, status, ok=False, email=None, message="", error_code=None, retryable=None, **kwargs):
    return {
        "status": status,
        "ok": ok,
        "email": email,
        "message": message,
        "error_code": error_code,
        "retryable": retryable,
    }


def _load_function(path, function_name):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name]
    namespace = {
        "sms_provider": _SmsProvider,
        "logger": _Logger(),
        "AccountUnusableError": type("AccountUnusableError", (Exception,), {}),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function_name], namespace


def _protocol_module(raise_error):
    proto = types.ModuleType("core.codex_oauth")
    proto._cfg = types.SimpleNamespace(ENABLE_CODEX_AUTO=True)

    def _raise_auth_source():
        raise raise_error

    proto._codex_auth_url_source = _raise_auth_source
    proto._codex_result = _codex_result
    return proto


def _run_roxy(code):
    function, namespace = _load_function(ROOT / "roxy_codex_oauth.py", "_run_roxy_codex_oauth_once")
    error = _SmsProviderError(code)
    proto = _protocol_module(error)
    opened = types.SimpleNamespace(raw={}, profile_id="profile-fixture")
    namespace.update(
        {
            "_roxy_cfg": types.SimpleNamespace(ROXY_SELENIUM_TIMEOUT=10),
            "_CODEX_BROWSER_KIND": contextvars.ContextVar("test_roxy_kind", default="Roxy"),
            "_detect_browser_kind": lambda opened: "Roxy",
            "wait_for_otp": lambda *args, **kwargs: None,
            "RoxyBrowserClient": object,
            "_build_driver": lambda opened: object(),
            "_center_browser_window": lambda driver: None,
            "clear_roxy_browser_auth_state": lambda driver: None,
            "_fill_email_and_otp": lambda *args, **kwargs: None,
            "human_delay": lambda *args, **kwargs: None,
        }
    )
    with patch.dict(sys.modules, {"core.codex_oauth": proto}):
        return function(
            "user@example.com",
            otp_provider=lambda *args, **kwargs: None,
            force=True,
            existing_driver=object(),
            existing_opened=opened,
            reuse_existing_profile=True,
        )


def _run_browser_use(code):
    function, namespace = _load_function(ROOT / "browser_use_codex_oauth.py", "_run_browser_use_codex_oauth_once")
    error = _SmsProviderError(code)
    proto = _protocol_module(error)
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None
    playwright.sync_api = sync_api

    class _Client:
        def open_session(self):
            return types.SimpleNamespace(connect_url="", proxy_country_code="", profile_id="")

    class _StepTimer:
        def __init__(self, *args, **kwargs):
            pass

        def done(self, *args, **kwargs):
            pass

    namespace.update(
        {
            "_cfg": types.SimpleNamespace(ENABLE_CODEX_AUTO=True, BROWSER_USE_KEEP_BROWSER_OPEN=False),
            "BrowserUseClient": _Client,
            "_StepTimer": _StepTimer,
            "_set_log_provider_label": lambda label: None,
        }
    )
    with patch.dict(
        sys.modules,
        {
            "core.codex_oauth": proto,
            "playwright": playwright,
            "playwright.sync_api": sync_api,
        },
    ):
        return function(
            "user@example.com",
            otp_provider=lambda *args, **kwargs: None,
            force=True,
        )


class BrowserCodexSmsResultTests(unittest.TestCase):
    def test_roxy_preserves_blocked_and_retryable_sms_outcomes(self):
        for code, expected_status, expected_retryable in (
            ("sms_no_balance", "blocked", False),
            ("sms_no_numbers", "failed", True),
        ):
            with self.subTest(driver="roxy", code=code):
                result = _run_roxy(code)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["retryable"], expected_retryable)
                self.assertFalse(result["ok"])

    def test_browser_use_preserves_blocked_and_retryable_sms_outcomes(self):
        for code, expected_status, expected_retryable in (
            ("sms_no_balance", "blocked", False),
            ("sms_no_numbers", "failed", True),
        ):
            with self.subTest(driver="browser_use", code=code):
                result = _run_browser_use(code)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["retryable"], expected_retryable)
                self.assertFalse(result["ok"])

    def test_browser_use_accepts_only_confirmed_otp_states(self):
        helper, _ = _load_function(ROOT / "browser_use_codex_oauth.py", "_otp_outcome_accepted")
        for outcome in ("accepted", "callback"):
            with self.subTest(outcome=outcome):
                self.assertTrue(helper(outcome))
        for outcome in ("unknown", "invalid", "still_phone_page", "", None):
            with self.subTest(outcome=outcome):
                self.assertFalse(helper(outcome))


if __name__ == "__main__":
    unittest.main()
