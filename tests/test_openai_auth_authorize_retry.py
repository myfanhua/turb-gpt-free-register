# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch, call

import core.openai_auth as openai_auth


class _Session:
    def __init__(self):
        self.calls = 0
        self.reset_count = 0
        self.cookie_jar_identity = object()

    def get_auth_navigate_headers(self, **_kwargs):
        return {"sec-fetch-mode": "navigate"}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            error = RuntimeError("HTTP Error 403")
            error.response = SimpleNamespace(status_code=403)
            raise error
        return SimpleNamespace(
            status_code=200,
            url="https://auth.openai.com/email-verification",
            raise_for_status=lambda: None,
        )

    def reset_circuit_breaker(self):
        self.reset_count += 1


class AuthorizeRetryTests(unittest.TestCase):
    def test_403_retries_in_same_session_without_replacing_cookie_jar(self):
        session = _Session()
        cookies = session.cookie_jar_identity
        with patch.object(openai_auth, "_proxy_retry_config", return_value=(3, 0.1)), \
             patch.object(openai_auth, "_interruptible_sleep") as sleep:
            url = openai_auth.follow_authorize(session, "https://auth.openai.com/authorize?state=test")
        self.assertEqual(url, "https://auth.openai.com/email-verification")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.reset_count, 1)
        self.assertIs(session.cookie_jar_identity, cookies)
        sleep.assert_called_once_with(0.1)

    def test_retryable_errors_have_bounded_attempts_and_backoff(self):
        for status in (403, 429, 500, 503):
            with self.subTest(status=status):
                session = _Session()
                error = RuntimeError(f"HTTP Error {status}")
                error.response = SimpleNamespace(status_code=status)
                with patch.object(session, "get", side_effect=error) as get, \
                     patch.object(openai_auth, "_proxy_retry_config", return_value=(3, 0.1)), \
                     patch.object(openai_auth, "_interruptible_sleep") as sleep:
                    with self.assertRaises(RuntimeError) as raised:
                        openai_auth.follow_authorize(session, "https://auth.openai.com/authorize")
                self.assertIs(raised.exception, error)
                self.assertEqual(get.call_count, 3)
                self.assertEqual(session.reset_count, 2)
                self.assertEqual(sleep.call_args_list, [call(0.1), call(0.2)])

    def test_non_retryable_business_error_stops_immediately(self):
        session = _Session()
        error = RuntimeError("HTTP Error 400")
        error.response = SimpleNamespace(status_code=400)
        with patch.object(session, "get", side_effect=error) as get, \
             patch.object(openai_auth, "_interruptible_sleep") as sleep:
            with self.assertRaises(RuntimeError):
                openai_auth.follow_authorize(session, "https://auth.openai.com/authorize")
        get.assert_called_once()
        self.assertEqual(session.reset_count, 0)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
