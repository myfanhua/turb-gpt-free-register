# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlparse

from core.chatgpt_auth import _ensure_authorize_context


class _Session:
    device_id = "did-123"
    auth_session_logging_id = "log-456"


class ChatgptAuthContextTests(unittest.TestCase):
    def test_server_context_is_preserved_including_duplicate_and_blank_values(self):
        query = ('state=s&ccaps=new_cap&ccaps=another&ui_locales=ja-JP'
                 '&auth_return_target_category=custom&ext-passkey-client-capabilities=server&screen_hint=')
        out = _ensure_authorize_context('https://auth.openai.com/authorize?' + query,
                                        _Session(), 'user@example.com')
        qs = parse_qs(urlparse(out).query, keep_blank_values=True)
        original = parse_qs(query, keep_blank_values=True)
        for key, value in original.items():
            self.assertEqual(qs[key], value)
        self.assertEqual(qs['login_hint'], ['user@example.com'])

    def test_unrelated_host_is_not_modified(self):
        for host in ('notauth.openai.com', 'auth.openai.com.evil', 'chatgpt.com'):
            url = f'https://{host}/authorize?state=s'
            self.assertEqual(_ensure_authorize_context(url, _Session(), 'a@example.com'), url)

    def test_ensure_authorize_context_matches_20260914_capture_shape(self):
        url = "https://auth.openai.com/api/accounts/authorize?client_id=app_x&state=s"
        out = _ensure_authorize_context(url, _Session(), "user@example.com")
        qs = parse_qs(urlparse(out).query)
        self.assertEqual(qs["ext-oai-did"], ["did-123"])
        self.assertEqual(qs["auth_session_logging_id"], ["log-456"])
        self.assertNotIn("ext-passkey-client-capabilities", qs)
        self.assertEqual(qs["screen_hint"], ["login_or_signup"])
        self.assertEqual(qs["login_hint"], ["user@example.com"])
        self.assertEqual(qs["ccaps"], ["login_methods chatgpt_login_finalizer_v1"])
        self.assertEqual(qs["auth_return_target_category"], ["chatgpt_home"])


if __name__ == "__main__":
    unittest.main()
