# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.codex_oauth as codex


class _CodexSession:
    def __init__(self):
        self.calls = []
        self.reset_count = 0
        self.responses = [
            SimpleNamespace(status_code=403, text="blocked"),
            SimpleNamespace(status_code=200, text="ok", url="https://auth.openai.com/log-in"),
        ]

    def get_auth_navigate_headers(self, **kwargs):
        self.header_kwargs = kwargs
        return {"sec-fetch-site": "none"}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def reset_circuit_breaker(self):
        self.reset_count += 1


class CodexOauthFingerprintTests(unittest.TestCase):
    def test_protocol_fingerprint_warmup_reuses_session_and_observes_document(self):
        class Session:
            def __init__(self):
                self.calls = []
                self.observed = []
                self.reset_count = 0

            def get_chatgpt_navigate_headers(self, **kwargs):
                self.header_kwargs = kwargs
                return {"sec-fetch-site": "none"}

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return SimpleNamespace(status_code=200, text="<html></html>", url=url)

            def observe_chatgpt_document(self, response):
                self.observed.append(response)

            def reset_circuit_breaker(self):
                self.reset_count += 1

        session = Session()
        with patch("core.chatgpt_bootstrap.anonymous_bootstrap") as bootstrap:
            codex._codex_protocol_fingerprint_warmup(session)

        self.assertEqual(session.calls[0][0], "https://chatgpt.com/auth/login")
        self.assertEqual(session.header_kwargs, {"referer": "", "user_initiated": False})
        self.assertEqual(len(session.observed), 1)
        self.assertEqual(session.observed[0].url, "https://chatgpt.com/auth/login")
        bootstrap.assert_called_once_with(session, strict=False)
        self.assertEqual(session.reset_count, 1)

    def test_auth_preflight_retries_403_in_same_session(self):
        session = _CodexSession()
        with patch.object(codex, "_interruptible_sleep") as sleep:
            codex._codex_auth_preflight(session)

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.reset_count, 1)
        self.assertEqual(session.header_kwargs["referer"], "")
        self.assertFalse(session.header_kwargs["user_initiated"])
        sleep.assert_called_once_with(1.0)

    def test_bootstrap_authorize_uses_external_top_level_navigation_headers(self):
        session = _CodexSession()
        session.responses = [
            SimpleNamespace(status_code=200, text="ok", url="https://auth.openai.com/log-in")
        ]
        session.device_id = "device-id"
        session.auth_session_logging_id = "logging-id"

        codex._bootstrap_authorize(
            session,
            "state",
            auth_url="https://auth.openai.com/oauth/authorize?state=state",
        )

        self.assertEqual(session.header_kwargs["referer"], "")
        self.assertTrue(session.header_kwargs["user_initiated"])
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
