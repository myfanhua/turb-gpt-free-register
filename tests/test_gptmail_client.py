# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import gptmail_client


class GPTMailClientTests(unittest.TestCase):
    def setUp(self):
        gptmail_client._CONTEXT_CACHE.clear()

    def test_pick_account_requires_configured_api_key(self):
        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "", create=True):
            with self.assertRaisesRegex(gptmail_client.GPTMailError, "请填写 GPTMail API Key"):
                gptmail_client.pick_account()

    @patch("core.gptmail_client.requests.get")
    def test_pick_account_generates_random_mailbox_with_key(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "success": True,
            "data": {"email": "fresh@gptmail.test"},
        }
        get.return_value = response

        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123", create=True):
            account = gptmail_client.pick_account()

        self.assertEqual(account.email, "fresh@gptmail.test")
        get.assert_called_once_with(
            "https://mail.chatgpt.org.uk/api/generate-email",
            headers={"Accept": "application/json", "X-API-Key": "key-123"},
            params=None,
            timeout=20,
        )

    @patch("core.gptmail_client.time.sleep")
    @patch("core.gptmail_client.requests.get")
    def test_fetch_latest_otp_reads_only_new_openai_email(self, get, sleep):
        inbox = Mock(status_code=200)
        inbox.json.return_value = {
            "success": True,
            "data": {
                "emails": [
                    {
                        "id": "old",
                        "timestamp": 100,
                        "from_address": "noreply@openai.com",
                        "subject": "Code 111111",
                    },
                    {
                        "id": "new",
                        "timestamp": 205,
                        "from_address": "noreply@openai.com",
                        "subject": "Code 654321",
                    },
                ]
            },
        }
        detail = Mock(status_code=200)
        detail.json.return_value = {
            "success": True,
            "data": {
                "id": "new",
                "timestamp": 205,
                "from_address": "noreply@openai.com",
                "subject": "Code 654321",
                "content": "Your code is 654321",
            },
        }
        get.side_effect = [inbox, detail]

        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123", create=True):
            code = gptmail_client.fetch_latest_otp(
                "fresh@gptmail.test",
                after_ts=200,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")

    @patch("core.gptmail_client.time.monotonic")
    @patch("core.gptmail_client.requests.get")
    def test_probe_does_not_reset_settle_for_same_message(self, get, monotonic):
        inbox = Mock(status_code=200)
        inbox.json.return_value = {
            "success": True,
            "data": {"emails": [{
                "id": "same",
                "timestamp": 205,
                "from_address": "noreply@openai.com",
                "subject": "Code 654321",
            }]},
        }
        detail = Mock(status_code=200)
        detail.json.return_value = {
            "success": True,
            "data": {
                "id": "same",
                "timestamp": 205,
                "from_address": "noreply@openai.com",
                "subject": "Code 654321",
                "content": "Your code is 654321",
            },
        }
        get.side_effect = lambda url, **kwargs: inbox if url.endswith("/emails") else detail
        monotonic.side_effect = iter([0, 0, 3, 6])
        state = gptmail_client.GPTMailProbeState()
        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123", create=True):
            first = gptmail_client.fetch_otp_once("fresh@gptmail.test", 200, state, settle_seconds=5)
            second = gptmail_client.fetch_otp_once("fresh@gptmail.test", 200, state, settle_seconds=5)
            third = gptmail_client.fetch_otp_once("fresh@gptmail.test", 200, state, settle_seconds=5)

        self.assertEqual((first.status, second.status, third.status), ("candidate", "candidate", "completed"))
        self.assertEqual(third.code, "654321")

    @patch("core.gptmail_client.requests.get")
    def test_probe_pending_and_after_ts_filter(self, get):
        inbox = Mock(status_code=200)
        inbox.json.return_value = {"success": True, "data": {"emails": [{
            "id": "old", "timestamp": 100, "from_address": "noreply@openai.com", "subject": "Code 111111",
        }]}}
        get.return_value = inbox
        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123", create=True):
            result = gptmail_client.fetch_otp_once("fresh@gptmail.test", 200, gptmail_client.GPTMailProbeState(), settle_seconds=0)
        self.assertEqual(result.status, "pending")
        self.assertEqual(get.call_count, 1)

    @patch("core.gptmail_client.requests.get")
    def test_pick_account_reports_api_error_message(self, get):
        response = Mock(status_code=401)
        response.json.return_value = {"success": False, "error": "Invalid API key"}
        get.return_value = response

        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "bad-key", create=True):
            with self.assertRaisesRegex(gptmail_client.GPTMailError, "Invalid API key"):
                gptmail_client.pick_account()
