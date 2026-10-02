import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).parents[1] / "core" / "codex_oauth.py"


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


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


def _load_run():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    wanted = {"_codex_result", "run_codex_oauth"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "_cfg": types.SimpleNamespace(ENABLE_CODEX_AUTO=True),
        "sms_provider": _SmsProvider,
        "logger": _Logger(),
        "uuid": __import__("uuid"),
        "AccountUnusableError": type("AccountUnusableError", (Exception,), {}),
        "_is_cpa_callback_reauth_error": lambda exc: False,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class CodexSmsResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = _load_run()

    def _run_with_sms_error(self, code):
        error = _SmsProviderError(code)

        class _BrowserSession:
            def __init__(self, *args, **kwargs):
                return None

            device_id = "device"
            oai_session_id = "session"
            auth_session_logging_id = "auth"

            def fingerprint_summary_text(self):
                return "fixture"

        def _raise_during_warmup(session):
            raise error

        config = types.ModuleType("config")
        config.codex = types.SimpleNamespace(CODEX_OAUTH_DRIVER="protocol")
        config.roxybrowser = types.SimpleNamespace(REGISTRATION_DRIVER="protocol")
        email_provider = types.ModuleType("core.email_provider")
        email_provider.wait_for_otp = lambda *args, **kwargs: None
        self.namespace["BrowserSession"] = _BrowserSession
        self.namespace["_codex_auth_url_source"] = lambda: "local"
        self.namespace["_generate_pkce"] = lambda: ("verifier", "challenge")
        self.namespace["_generate_state"] = lambda: "state"
        self.namespace["_codex_protocol_fingerprint_warmup"] = _raise_during_warmup
        with patch.dict(sys.modules, {"config": config, "core.email_provider": email_provider}):
            return self.namespace["run_codex_oauth"]("user@example.com", force=True)

    def test_no_balance_is_blocked_and_non_retryable(self):
        result = self._run_with_sms_error("sms_no_balance")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "sms_no_balance")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["ok"])

    def test_no_numbers_remains_retryable_failure(self):
        result = self._run_with_sms_error("sms_no_numbers")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "sms_no_numbers")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
