# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import cf_temp_mail_client as client


class CFTempMailClientTests(unittest.TestCase):
    def setUp(self):
        client._CONTEXT_CACHE.clear()
        client._DOMAIN_COUNTER = 0

    def test_pick_account_requires_api_base(self):
        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "", create=True):
            with self.assertRaisesRegex(client.CFTempMailError, "请填写 CLOUDFLARE_API_BASE"):
                client.pick_account()

    @patch("core.cf_temp_mail_client.requests.request")
    def test_pick_account_anonymous_create(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {
            "address": "abc123@mail.example.com",
            "jwt": "jwt-token-1",
        }
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["mail.example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True
        ):
            account = client.pick_account()

        self.assertEqual(account.email, "abc123@mail.example.com")
        self.assertEqual(account.jwt, "jwt-token-1")
        self.assertIs(client.get_account_context(account.email), account)
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://mail.example.com/api/new_address")
        self.assertEqual(kwargs["json"], {"domain": "mail.example.com"})

    @patch("core.cf_temp_mail_client.requests.request")
    def test_admin_create_uses_name_payload_and_header(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {"address": "u@mail.example.com", "jwt": "jwt-2"}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "x-admin-auth", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "admin-pass", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/admin/new_address", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", ["mail.example.com"], create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "global-pass", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_NAME_LENGTH", 10, create=True):
            account = client.pick_account()

        self.assertEqual(account.email, "u@mail.example.com")
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["headers"]["x-admin-auth"], "admin-pass")
        self.assertEqual(kwargs["headers"]["x-custom-auth"], "global-pass")
        self.assertEqual(kwargs["json"]["enablePrefix"], True)
        self.assertEqual(kwargs["json"]["domain"], "mail.example.com")
        self.assertIn("name", kwargs["json"])

    @patch("core.cf_temp_mail_client.time.sleep")
    @patch("core.cf_temp_mail_client.requests.request")
    def test_fetch_latest_otp_reads_only_new_openai_email(self, request_mock, sleep):
        client._CONTEXT_CACHE["fresh@mail.example.com"] = client.CFTempMailAccount(
            email="fresh@mail.example.com",
            jwt="jwt-xyz",
            domain="mail.example.com",
        )

        inbox = Mock(status_code=200)
        inbox.json.return_value = {
            "results": [
                {
                    "id": "old",
                    "timestamp": 100,
                    "address": "fresh@mail.example.com",
                    "from": "noreply@openai.com",
                    "subject": "Code 111111",
                    "text": "Your code is 111111",
                },
                {
                    "id": "new",
                    "timestamp": 250,
                    "address": "fresh@mail.example.com",
                    "from": "noreply@openai.com",
                    "subject": "Code 654321",
                    "text": "Your code is 654321",
                },
            ]
        }
        request_mock.return_value = inbox

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_MESSAGES", "/api/mails", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True):
            code = client.fetch_latest_otp(
                "fresh@mail.example.com",
                after_ts=200,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/api/mails"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-xyz")

    @patch("core.cf_temp_mail_client.time.monotonic", side_effect=[0, 0, 3, 6])
    @patch("core.cf_temp_mail_client.list_messages")
    def test_probe_candidate_settles_without_internal_sleep(self, list_messages, monotonic):
        account = client.CFTempMailAccount(email="fresh@mail.example.com", jwt="jwt-xyz")
        client._CONTEXT_CACHE[account.email] = account
        list_messages.return_value = [{
            "id": "new", "timestamp": 250, "address": account.email,
            "from": "noreply@openai.com", "subject": "Code 654321", "text": "Your code is 654321",
        }]
        state = client.CFTempMailProbeState()
        first = client.fetch_otp_once(account.email, 200, state, settle_seconds=5)
        second = client.fetch_otp_once(account.email, 200, state, settle_seconds=5)
        third = client.fetch_otp_once(account.email, 200, state, settle_seconds=5)
        self.assertEqual((first.status, second.status, third.status), ("candidate", "candidate", "completed"))
        self.assertEqual(third.code, "654321")

    @patch("core.cf_temp_mail_client.list_messages")
    def test_probe_pending_filters_old_messages(self, list_messages):
        account = client.CFTempMailAccount(email="fresh@mail.example.com", jwt="jwt-xyz")
        client._CONTEXT_CACHE[account.email] = account
        list_messages.return_value = [{
            "id": "old", "timestamp": 100, "address": account.email,
            "from": "noreply@openai.com", "subject": "Code 111111", "text": "Your code is 111111",
        }]
        result = client.fetch_otp_once(account.email, 200, client.CFTempMailProbeState(), settle_seconds=0)
        self.assertEqual(result.status, "pending")

    def test_release_clears_context(self):
        client._CONTEXT_CACHE["a@b.com"] = client.CFTempMailAccount(email="a@b.com", jwt="t")
        client.release_account("a@b.com", status="used")
        self.assertIsNone(client.get_account_context("a@b.com"))

    def test_admin_mode_without_key_fails(self):
        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_AUTH_MODE", "x-admin-auth", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/admin/new_address", create=True
        ):
            with self.assertRaisesRegex(client.CFTempMailError, "CLOUDFLARE_API_KEY"):
                client.pick_account()


    @patch("core.cf_temp_mail_client.requests.request")
    def test_list_messages_sends_limit_offset(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {"results": []}
        request_mock.return_value = response

        with patch.object(client._email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_PATH_MESSAGES", "/api/mails", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_AUTH_MODE", "none", create=True), patch.object(
            client._email_cfg, "CLOUDFLARE_API_KEY", "", create=True
        ), patch.object(client._email_cfg, "CLOUDFLARE_CUSTOM_AUTH", "", create=True):
            client.list_messages("jwt-xyz")

        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/api/mails"))
        self.assertEqual(kwargs["params"]["limit"], 20)
        self.assertEqual(kwargs["params"]["offset"], 0)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-xyz")


    def test_created_at_without_tz_is_utc(self):
        from datetime import datetime, timezone
        ts = client._message_timestamp({"created_at": "2026-07-19 12:57:38"})
        expected = datetime(2026, 7, 19, 12, 57, 38, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(ts, expected, places=0)

    def test_otp_from_cloudflare_raw_openai_mail(self):
        raw = (
            "From: ChatGPT <noreply@tm.openai.com>\r\n"
            "Subject: ChatGPT code\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>Your code</p>\n449759\n</body></html>\r\n"
        )
        item = {
            "id": 77,
            "source": "bounces+x@em7877.tm.openai.com",
            "address": "user@beliefcode.online",
            "raw": raw,
            "created_at": "2026-07-19 12:57:38",
        }
        otp_item = client._otp_item(item)
        from core.otp_utils import looks_like_openai_email, extract_otp
        self.assertTrue(looks_like_openai_email(otp_item))
        self.assertEqual(extract_otp(otp_item), "449759")


if __name__ == "__main__":
    unittest.main()
