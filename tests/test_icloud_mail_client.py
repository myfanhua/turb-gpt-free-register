# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import requests

from core import icloud_mail_client as client


MESSAGE_DATE = "2026-07-31T03:10:00.000Z"
AFTER_TS = 1785467390


def response(status=200, *, email="one@icloud.com", to="one@icloud.com", subject="OpenAI code 654321", text="", html="", headers=None):
    item = Mock(status_code=status, headers=headers or {})
    item.json.return_value = {
        "email": email,
        "message": {
            "uid": 7,
            "to": to,
            "date": MESSAGE_DATE,
            "from": "noreply@openai.com",
            "subject": subject,
            "text": text,
            "html": html,
        },
    }
    return item


class ICloudMailClientTests(unittest.TestCase):
    def setUp(self):
        client._CONTEXT_CACHE.clear()
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="tok_one_1234",
        )

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_sends_mailbox_specific_headers(self, get):
        get.return_value = response(text="Your code is 654321")

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=AFTER_TS,
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        get.assert_called_once_with(
            "https://icloud.flysms.top/icloud/api/pickup/messages/latest",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer tok_one_1234",
                "X-Mailbox-Email": "one@icloud.com",
                "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
            },
            timeout=15,
        )

    @patch("core.icloud_mail_client.requests.get")
    def test_each_mailbox_uses_its_own_headers(self, get):
        client._CONTEXT_CACHE["two@icloud.com"] = client.ICloudMailAccount(
            email="two@icloud.com",
            token="tok_two_5678",
        )
        get.side_effect = [
            response(subject="OpenAI code 111111"),
            response(email="two@icloud.com", to="two@icloud.com", subject="OpenAI code 222222"),
        ]

        self.assertEqual(client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0), "111111")
        self.assertEqual(client.fetch_latest_otp("two@icloud.com", AFTER_TS, 1, 1, 0), "222222")

        first_headers = get.call_args_list[0].kwargs["headers"]
        second_headers = get.call_args_list[1].kwargs["headers"]
        self.assertEqual(first_headers["X-Mailbox-Email"], "one@icloud.com")
        self.assertEqual(first_headers["Authorization"], "Bearer tok_one_1234")
        self.assertEqual(second_headers["X-Mailbox-Email"], "two@icloud.com")
        self.assertEqual(second_headers["Authorization"], "Bearer tok_two_5678")

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_rejects_response_for_another_mailbox(self, get):
        get.return_value = response(email="two@icloud.com", to="two@icloud.com", subject="Code 111111")

        with self.assertRaisesRegex(client.ICloudMailError, "响应邮箱不匹配"):
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_rejects_response_for_another_recipient(self, get):
        get.return_value = response(to=["two@icloud.com"], subject="Code 111111")

        with self.assertRaisesRegex(client.ICloudMailError, "收件人不匹配"):
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

    @patch("core.icloud_mail_client.requests.get")
    def test_old_message_is_not_accepted(self, get):
        old = response(subject="OpenAI code 111111")
        old.json.return_value["message"]["date"] = "2026-07-31T03:00:00.000Z"
        get.return_value = old

        with self.assertRaisesRegex(client.ICloudMailError, "早于本次验证码请求"):
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

    @patch("core.icloud_mail_client.requests.get")
    def test_extracts_otp_from_html(self, get):
        get.return_value = response(
            subject="Verify your email",
            html="<p>Your verification code is <strong>765432</strong></p>",
        )

        self.assertEqual(
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0),
            "765432",
        )

    @patch("core.icloud_mail_client.db.release_icloud_email")
    @patch("core.icloud_mail_client.requests.get")
    def test_401_disables_mailbox_without_exposing_token(self, get, release):
        item = Mock(status_code=401, headers={})
        item.json.return_value = {"error": "Invalid pickup credentials"}
        get.return_value = item

        with self.assertRaisesRegex(client.ICloudMailError, "401") as raised:
            client.fetch_latest_otp("one@icloud.com", 0, 0, 1, 0)

        self.assertNotIn("tok_one_1234", str(raised.exception))
        release.assert_called_once_with(
            "one@icloud.com",
            status="disabled",
            note="iCloud Pickup HTTP 401",
        )

    @patch("core.icloud_mail_client.db.release_icloud_email")
    @patch("core.icloud_mail_client.requests.get")
    def test_403_disables_mailbox(self, get, release):
        get.return_value = Mock(status_code=403, headers={})

        with self.assertRaisesRegex(client.ICloudMailError, "403"):
            client.fetch_latest_otp("one@icloud.com", 0, 0, 1, 0)

        release.assert_called_once_with(
            "one@icloud.com",
            status="disabled",
            note="iCloud Pickup HTTP 403",
        )

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_404_then_new_message_returns_otp(self, get, sleep):
        empty = Mock(status_code=404, headers={})
        empty.json.return_value = {"error": "No messages"}
        found = response(to=["one@icloud.com"], subject="OpenAI code 222222")
        get.side_effect = [empty, found]

        code = client.fetch_latest_otp("one@icloud.com", AFTER_TS, 5, 1, 0)

        self.assertEqual(code, "222222")
        self.assertEqual(sleep.call_count, 1)

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_429_uses_retry_after_before_retrying(self, get, sleep):
        limited = Mock(status_code=429, headers={"Retry-After": "4"})
        limited.json.return_value = {"error": "Too many requests"}
        get.side_effect = [limited, response(subject="OpenAI code 333333")]

        code = client.fetch_latest_otp("one@icloud.com", AFTER_TS, 5, 1, 0)

        self.assertEqual(code, "333333")
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 3.0)

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_503_is_retried(self, get, sleep):
        unavailable = Mock(status_code=503, headers={})
        unavailable.json.return_value = {"error": "Initializing"}
        get.side_effect = [unavailable, response(subject="OpenAI code 444444")]

        code = client.fetch_latest_otp("one@icloud.com", AFTER_TS, 5, 1, 0)

        self.assertEqual(code, "444444")
        self.assertEqual(sleep.call_count, 1)

    @patch("core.icloud_mail_client.requests.get")
    def test_network_error_is_reported_without_exposing_token(self, get):
        get.side_effect = requests.ConnectionError("network down for tok_one_1234")

        with self.assertRaisesRegex(client.ICloudMailError, r"ConnectionError: network down for \*\*\*") as raised:
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

        self.assertNotIn("tok_one_1234", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
