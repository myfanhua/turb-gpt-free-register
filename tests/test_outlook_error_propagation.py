import sys
import types
import unittest
from unittest.mock import MagicMock, patch

try:
    from curl_cffi.requests import Session as _CurlSession
except ModuleNotFoundError:
    class _CookieJar:
        def __init__(self):
            self.jar = []

        def set(self, name, value, domain="", path="/"):
            self.jar = [
                cookie for cookie in self.jar
                if not (getattr(cookie, "name", "") == name and getattr(cookie, "domain", "") == domain)
            ]
            self.jar.append(types.SimpleNamespace(name=name, value=value, domain=domain, path=path))

    class _CurlSession:
        def __init__(self, *args, **kwargs):
            self.cookies = _CookieJar()
            self.proxies = {}
            self.timeout = None

        def close(self):
            return None

    _requests = types.ModuleType("curl_cffi.requests")
    _requests.Session = _CurlSession
    _curl = types.ModuleType("curl_cffi")
    _curl.requests = _requests
    sys.modules["curl_cffi"] = _curl
    sys.modules["curl_cffi.requests"] = _requests

from core import outlook_client as outlook


class OutlookErrorPropagationTests(unittest.TestCase):
    def setUp(self):
        self.account = outlook.OutlookAccount(
            email="otp@example.com",
            password="",
            client_id="client",
            refresh_token="refresh",
        )

    def _run_poll(self, fetch_side_effect=None, fetch_return=None):
        clock_values = iter((100.0, 100.0, 101.0))
        def clock():
            try:
                return next(clock_values)
            except StopIteration:
                return 101.0
        fetch = patch.object(outlook, "_fetch_via", side_effect=fetch_side_effect, return_value=fetch_return)
        with patch.object(outlook, "get_account_context", return_value=self.account),              patch.object(outlook, "_http_session", return_value=MagicMock()),              patch.object(outlook.time, "time", side_effect=clock),              patch.object(outlook.time, "sleep"):
            with fetch:
                return outlook.fetch_latest_otp(
                    self.account.email,
                    max_wait=1,
                    poll_interval=1,
                    settle_seconds=0,
                )

    def test_fetch_failure_survives_final_otp_poll(self):
        error = outlook.OutlookClientError(
            "refresh token rejected",
            error_code="outlook_oauth_refresh_failed",
            stage="oauth",
            retryable=False,
        )
        with self.assertRaises(outlook.OutlookClientError) as caught:
            self._run_poll(fetch_side_effect=[error, error])
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.error_code, "outlook_oauth_refresh_failed")
        self.assertEqual(caught.exception.stage, "oauth")
        self.assertFalse(caught.exception.retryable)

    def test_empty_mailbox_without_fetch_error_is_otp_timeout(self):
        with self.assertRaises(outlook.OutlookClientError) as caught:
            self._run_poll(fetch_return=[])
        self.assertEqual(caught.exception.error_code, "otp_timeout")
        self.assertEqual(caught.exception.stage, "otp_poll")
        self.assertTrue(caught.exception.retryable)

    def test_error_helper_adds_context_to_unstructured_exception(self):
        error = outlook._ensure_outlook_error(
            RuntimeError("socket closed"),
            stage="imap_fetch",
            error_code="outlook_imap_fetch_error",
            retryable=True,
        )
        self.assertEqual(error.error_code, "outlook_imap_fetch_error")
        self.assertEqual(error.stage, "imap_fetch")
        self.assertTrue(error.retryable)
        self.assertIn("socket closed", str(error))


if __name__ == "__main__":
    unittest.main()
