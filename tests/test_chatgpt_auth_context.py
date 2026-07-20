# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlparse

from core.chatgpt_auth import _ensure_authorize_context


class _Session:
    device_id = "did-123"
    auth_session_logging_id = "log-456"


class ChatgptAuthContextTests(unittest.TestCase):
    def test_ensure_authorize_context_matches_20260719_capture_shape(self):
        url = "https://auth.openai.com/api/accounts/authorize?client_id=app_x&state=s"
        out = _ensure_authorize_context(url, _Session(), "user@example.com")
        qs = parse_qs(urlparse(out).query)
        self.assertEqual(qs["ext-oai-did"], ["did-123"])
        self.assertEqual(qs["auth_session_logging_id"], ["log-456"])
        self.assertEqual(qs["ext-passkey-client-capabilities"], ["11111"])
        self.assertEqual(qs["screen_hint"], ["login_or_signup"])
        self.assertEqual(qs["login_hint"], ["user@example.com"])
        self.assertEqual(qs["ccaps"], ["login_methods"])


if __name__ == "__main__":
    unittest.main()
