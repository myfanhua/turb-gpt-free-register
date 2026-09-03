# -*- coding: utf-8 -*-
import base64
import unittest
from unittest.mock import patch

from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    _decode_data_uri,
    _fetch_tibosb_otp,
    _fetch_yangyang_otp,
    _normalize_code_url,
    _parse_tibosb_share_url,
    _parse_yangyang_code_url,
    fetch_latest_otp,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/api/messages/" in url:
            return FakeResponse(data={
                "items": [
                    {"id": 1, "subject": "旧码", "received_at": "2026-08-01 10:00:00"},
                    {"id": 2, "subject": "Your OpenAI code is 654321", "received_at": "2026-08-01 10:01:00"},
                ],
                "has_more": False,
            })
        if "/message/2/" in url:
            html = "<html><body>Your verification code is <b>654321</b></body></html>"
            body = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode()
            return FakeResponse(data={"subject": "Your OpenAI code is 654321", "body": body, "receivedAt": "2026-08-01 10:01:00"})
        if "/message/1/" in url:
            return FakeResponse(data={"subject": "旧码", "body": "code 111111", "receivedAt": "2026-08-01 10:00:00"})
        return FakeResponse(status_code=404, text="not found")


class FakeInlineSession:
    def get(self, url, **kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        return FakeResponse(text="""
        <article class="mail-card">
          <details open>
            <summary>
              <span class="subject">Your temporary ChatGPT verification code</span>
              <span class="date">2026-08-02 13:18:53</span>
            </summary>
            <div class="meta">发件人：otp@example.com</div>
            <pre class="body">Enter this temporary verification code to continue:

541409

Please ignore this email.</pre>
          </details>
        </article>
        """)


class FakeTibosSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/inbox?" in url:
            return FakeResponse(data={
                "success": True,
                "data": {
                    "messages": [
                        {
                            "id": "new-uid",
                            "from": "noreply@openai.com",
                            "subject": "Your temporary ChatGPT verification code",
                            "date": "2026-09-02T18:30:00Z",
                            "preview": "Open this message to see the code",
                            "folder": "INBOX",
                        }
                    ]
                },
            })
        if "/message?" in url:
            return FakeResponse(data={
                "success": True,
                "data": {
                    "id": "new-uid",
                    "subject": "Your temporary ChatGPT verification code",
                    "date": "2026-09-02T18:30:00Z",
                    "body": "Your verification code is 654321",
                },
            })
        return FakeResponse(status_code=404, text="not found")


class GenericApiYangyangTests(unittest.TestCase):
    def test_normalize_tibosb_share_page_to_public_api(self):
        self.assertEqual(
            _normalize_code_url("http://api.tibosb.cloud/share/token-123"),
            "http://api.tibosb.cloud/api/public/share/token-123",
        )

    def test_fetch_latest_otp_uses_normalized_tibosb_api(self):
        session = FakeTibosSession()
        account = GenericApiEmailAccount(
            email="fresh@example.com",
            code_url="http://api.tibosb.cloud/share/token-123",
        )

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ):
            code = fetch_latest_otp(
                account.email,
                after_ts=0.0,
                max_wait=5,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")
        self.assertIn("/api/public/share/token-123/inbox?", session.urls[0])
        self.assertTrue(any("/message?" in url for url in session.urls))

    def test_fetch_tibosb_share_otp_uses_inbox_and_message(self):
        self.assertEqual(
            _parse_tibosb_share_url("http://api.tibosb.cloud/share/token-123"),
            ("http://api.tibosb.cloud", "token-123"),
        )
        session = FakeTibosSession()
        code, meta = _fetch_tibosb_otp(
            session,
            "http://api.tibosb.cloud/share/token-123",
            {"User-Agent": "test"},
        )
        self.assertEqual(code, "654321")
        self.assertEqual(meta["mail_id"], "new-uid")

    def test_parse_yangyang_url(self):
        self.assertEqual(
            _parse_yangyang_code_url("http://yangyang.website/messages/tok/a@icloud.com"),
            ("http://yangyang.website", "tok", "a@icloud.com"),
        )

    def test_decode_data_uri_base64(self):
        body = "data:text/html;base64," + base64.b64encode("验证码 123456".encode()).decode()
        self.assertIn("123456", _decode_data_uri(body))

    def test_fetch_yangyang_otp_uses_api_and_detail(self):
        result = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "654321")
        self.assertEqual(meta["mail_id"], 2)

    def test_fetch_yangyang_otp_respects_after_ts(self):
        import datetime
        after = datetime.datetime(2026, 8, 1, 10, 2, 0).timestamp()
        result = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
            after_ts=after,
        )
        self.assertIsNone(result)

    def test_fetch_inline_messages_page_without_api(self):
        result = _fetch_yangyang_otp(
            FakeInlineSession(),
            "https://mail.ai1998.xyz/messages/tok/cookies-benzene.48%40icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "541409")
        self.assertEqual(meta["mail_id"], "inline-0")


if __name__ == "__main__":
    unittest.main()
