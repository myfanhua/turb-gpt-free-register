# -*- coding: utf-8 -*-
"""已保存 ChatGPT Web 会话的受控续期：所有 HTTP 均为 mock。"""
import base64
import json
import unittest
from unittest.mock import patch

from core import session_refresh


def fake_jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class _Cookies(dict):
    def get_dict(self):
        return dict(self)


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, response):
        self.cookies = _Cookies()
        self.response = response
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


class SessionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "id": 17,
            "access_token": fake_jwt(1893456000),
            "extra_json": json.dumps({"auth_artifacts": {"session": {"expires": "2030-01-01T00:00:00Z"}, "cookies": {"session": "fixture-cookie"}}}),
        }

    def test_token_status_distinguishes_session_attempt_from_oauth_refresh(self):
        status = session_refresh.token_status(self.account)
        self.assertEqual(status["refresh_mode"], "session_renewal")
        self.assertEqual(status["refresh_label"], "可尝试会话续期")
        self.assertTrue(status["session_renewal_possible"])
        self.assertFalse(status["oauth_refreshable"])
        self.assertTrue(status["expires_at"].endswith("Z"))

        oauth_account = dict(self.account)
        oauth_account["extra_json"] = json.dumps({"auth_artifacts": {"refresh_token": "fixture-oauth-refresh", "cookies": {"session": "fixture-cookie"}}})
        oauth_status = session_refresh.token_status(oauth_account)
        self.assertEqual(oauth_status["refresh_mode"], "oauth_refresh")
        self.assertTrue(oauth_status["oauth_refreshable"])

    def test_refresh_success_uses_only_session_endpoint_and_returns_redacted_status(self):
        payload = {"accessToken": fake_jwt(1893459600), "expires": "2030-01-01T01:00:00Z", "user": {"email": "fixture@example.test"}}
        fake_session = _Session(_Response(payload=payload))
        with patch("core.session_refresh.db.get_account", return_value=self.account), \
             patch("core.session_refresh.db.update_account_auth_session", return_value=True) as update, \
             patch("core.session_refresh.requests.Session", return_value=fake_session):
            result = session_refresh.refresh_account_session(17)
        self.assertTrue(result["ok"])
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["reason"], "session_renewed")
        self.assertNotIn("accessToken", result)
        self.assertNotIn("cookies", result)
        self.assertEqual(fake_session.requests[0][0], session_refresh.SESSION_ENDPOINT)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], 17)
        self.assertEqual(update.call_args.kwargs["token_expires_at"], result["status"]["expires_at"])

    def test_challenge_response_stops_without_persisting_or_bypass(self):
        fake_session = _Session(_Response(status_code=403, text="Turnstile challenge"))
        with patch("core.session_refresh.db.get_account", return_value=self.account), \
             patch("core.session_refresh.db.update_account_auth_session") as update, \
             patch("core.session_refresh.requests.Session", return_value=fake_session):
            result = session_refresh.refresh_account_session(17)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "browser_verification_required")
        self.assertEqual(result["http_status"], 403)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
