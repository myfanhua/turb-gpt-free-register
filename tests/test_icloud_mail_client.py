# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timedelta, timezone
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


def profile_response(
    status=200,
    *,
    account="one@icloud.com",
    to="one@icloud.com",
    subject="OpenAI code 654321",
    preview="Your code is 654321",
):
    item = Mock(status_code=status, headers={})
    item.json.return_value = {
        "mode": "reset",
        "cursor": "103595",
        "hasMore": False,
        "changes": [
            {
                "operation": "upsert",
                "account": account,
                "mailbox": "INBOX",
                "uid": 4295037043,
                "detailDeferred": True,
                "summary": {
                    "account": account,
                    "mailbox": "INBOX",
                    "uid": 4295037043,
                    "date": MESSAGE_DATE,
                    "from": "ChatGPT <noreply@tm.openai.com>",
                    "to": to,
                    "subject": subject,
                    "preview": preview,
                },
            }
        ],
    }
    return item


def html_response(
    status=200,
    *,
    mailbox="one@icloud.com",
    sender="ChatGPT <noreply@tm.openai.com>",
    subject="Your verification code is 654321",
    date=MESSAGE_DATE,
    body="Use verification code 654321",
):
    item = Mock(status_code=status, headers={})
    item.text = f"""
    <html><head><title>{mailbox}</title></head><body><div class="cnt">1 封</div>
      <div class="card">
        <div class="fr">{sender}</div>
        <div class="su">{subject}</div>
        <div class="dt">{date}</div>
        <div class="bd">{body}</div>
      </div>
    </body></html>
    """
    return item


class ICloudMailClientTests(unittest.TestCase):
    def setUp(self):
        self.profile_token = getattr(client._email_cfg, "ICLOUD_PROFILE_TOKEN", "")
        self.profile_api_base = getattr(client._email_cfg, "ICLOUD_PROFILE_API_BASE", "")
        client._email_cfg.ICLOUD_PROFILE_TOKEN = ""
        client._email_cfg.ICLOUD_PROFILE_API_BASE = "https://flysms.top/icloud/api"
        client._reset_profile_sync_cache()
        client._CONTEXT_CACHE.clear()
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="tok_one_1234",
        )

    def tearDown(self):
        client._reset_profile_sync_cache()
        client._email_cfg.ICLOUD_PROFILE_TOKEN = self.profile_token
        client._email_cfg.ICLOUD_PROFILE_API_BASE = self.profile_api_base

    @patch("core.icloud_mail_client.db.release_icloud_email")
    @patch("core.icloud_mail_client.db.list_icloud_email_pool")
    @patch("core.icloud_mail_client.db.claim_next_icloud_email")
    def test_pick_account_restores_pickup_disabled_mailbox_for_profile_mode(
        self,
        claim,
        list_pool,
        release,
    ):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        claim.side_effect = [None, {
            "id": 7,
            "email": "one@icloud.com",
            "token": "tok_one_1234",
            "pickup_url": "",
        }]
        list_pool.return_value = [{
            "email": "one@icloud.com",
            "status": "disabled",
            "note": "iCloud Pickup HTTP 401",
        }]

        account = client.pick_account()

        self.assertEqual(account.email, "one@icloud.com")
        release.assert_called_once_with(
            "one@icloud.com",
            status="available",
            note="已切换到 iCloud Profile 同步",
        )
        self.assertEqual(claim.call_count, 2)

    @patch("core.icloud_mail_client.db.claim_next_icloud_email")
    def test_pick_account_passes_requested_claim_selection(self, claim):
        claim.return_value = {
            "id": 7,
            "email": "one@icloud.com",
            "token": "tok_one",
            "pickup_url": "",
            "pickup_mode": "api_token",
            "claimed_pickup_mode": "api_token",
        }

        account = client.pick_account(selection="token")

        claim.assert_called_once_with(pickup_filter="token")
        self.assertEqual(account.pickup_mode, "api_token")

    @patch("core.icloud_mail_client.db.get_icloud_email_by_email")
    def test_context_restores_forced_api_mode_without_browser_url(self, get_row):
        get_row.return_value = {
            "email": "mixed@icloud.com",
            "token": "tok_mixed",
            "pickup_url": "https://pickup.example/show/secret/mixed@icloud.com",
            "pickup_mode": "independent_url_with_token",
            "claimed_pickup_mode": "api_token",
        }
        client._CONTEXT_CACHE.clear()

        account = client.get_account_context("mixed@icloud.com")

        self.assertEqual(account.token, "tok_mixed")
        self.assertEqual(account.pickup_url, "")
        self.assertEqual(account.pickup_mode, "api_token")

    @patch("core.icloud_mail_client.db.get_icloud_email_by_email")
    def test_context_restores_forced_url_mode_without_api_token(self, get_row):
        pickup_url = "https://pickup.example/show/secret/mixed@icloud.com"
        get_row.return_value = {
            "email": "mixed@icloud.com",
            "token": "tok_mixed",
            "pickup_url": pickup_url,
            "pickup_mode": "independent_url_with_token",
            "claimed_pickup_mode": "independent_url",
        }
        client._CONTEXT_CACHE.clear()

        account = client.get_account_context("mixed@icloud.com")

        self.assertEqual(account.token, "")
        self.assertEqual(account.pickup_url, pickup_url)
        self.assertEqual(account.pickup_mode, "independent_url")

    @patch("core.icloud_mail_client.requests.post")
    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_prefers_direct_pickup_when_profile_token_is_configured(self, get, post):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        get.return_value = response(text="Your code is 654321")

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=AFTER_TS,
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        get.assert_called_once()
        post.assert_not_called()

    @patch("core.icloud_mail_client.db.release_icloud_email")
    @patch("core.icloud_mail_client.requests.post")
    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_falls_back_to_profile_when_direct_credentials_fail(self, get, post, release):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        get.return_value = Mock(status_code=401, headers={})
        post.return_value = profile_response()

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=AFTER_TS,
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        release.assert_not_called()
        post.assert_called_once_with(
            "https://flysms.top/icloud/api/mail/sync",
            headers={
                "Accept": "application/json",
                "X-Profile-Token": "profile_secret_1234",
                "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
            },
            json={},
            timeout=15,
        )

    @patch("core.icloud_mail_client.requests.get", return_value=Mock(status_code=401, headers={}))
    @patch("core.icloud_mail_client.requests.post")
    def test_profile_sync_selects_otp_when_newer_message_is_unrelated(self, post, get):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        item = profile_response()
        item.json.return_value["changes"].append({
            "operation": "upsert",
            "account": "one@icloud.com",
            "mailbox": "INBOX",
            "uid": 4295037044,
            "summary": {
                "account": "one@icloud.com",
                "mailbox": "INBOX",
                "uid": 4295037044,
                "date": "2026-07-31T03:11:00.000Z",
                "from": "newsletter@example.com",
                "to": "one@icloud.com",
                "subject": "Daily newsletter",
                "preview": "No verification code here",
            },
        })
        post.return_value = item

        self.assertEqual(
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0),
            "654321",
        )

    @patch("core.icloud_mail_client.requests.get", return_value=Mock(status_code=401, headers={}))
    @patch("core.icloud_mail_client.requests.post")
    def test_profile_sync_skips_incomplete_newer_candidate(self, post, get):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        item = profile_response()
        item.json.return_value["changes"].append({
            "operation": "upsert",
            "account": "one@icloud.com",
            "mailbox": "INBOX",
            "uid": 4295037044,
            "summary": {
                "account": "one@icloud.com",
                "mailbox": "INBOX",
                "uid": 4295037044,
                "date": "2026-07-31T03:11:00.000Z",
                "from": "ChatGPT <noreply@tm.openai.com>",
                "to": None,
                "subject": "OpenAI code 777777",
                "preview": "Your code is 777777",
            },
        })
        post.return_value = item

        self.assertEqual(
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0),
            "654321",
        )

    @patch("core.icloud_mail_client.requests.get", return_value=Mock(status_code=401, headers={}))
    @patch("core.icloud_mail_client.requests.post")
    def test_profile_sync_follows_cursor_until_target_message(self, post, get):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        first = Mock(status_code=200, headers={})
        first.json.return_value = {
            "mode": "reset",
            "cursor": "page-2",
            "hasMore": True,
            "changes": [],
        }
        second = profile_response()
        post.side_effect = [first, second]

        self.assertEqual(
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0),
            "654321",
        )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"], {})
        self.assertEqual(post.call_args_list[1].kwargs["json"], {"cursor": "page-2"})

    @patch("core.icloud_mail_client.requests.get", return_value=Mock(status_code=401, headers={}))
    @patch("core.icloud_mail_client.requests.post")
    def test_profile_network_error_hides_profile_token(self, post, get):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        post.side_effect = requests.ConnectionError("network down for profile_secret_1234")

        with self.assertRaisesRegex(
            client.ICloudMailError,
            r"ConnectionError: network down for \*\*\*",
        ) as raised:
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

        self.assertNotIn("profile_secret_1234", str(raised.exception))

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
            "https://flysms.top/icloud/api/pickup/messages/latest",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer tok_one_1234",
                "X-Mailbox-Email": "one@icloud.com",
                "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
            },
            timeout=15,
        )

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_uses_mailbox_pickup_url_when_present(self, get):
        get.return_value = response(text="Your code is 654321")
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="tok_one_1234",
            pickup_url="https://pickup.example/messages/latest?mail=one%40icloud.com",
        )

        self.assertEqual(client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0), "654321")
        self.assertEqual(get.call_args.args[0], "https://pickup.example/messages/latest?mail=one%40icloud.com")

    @patch("core.icloud_mail_client.requests.post")
    @patch("core.icloud_mail_client.requests.get")
    def test_url_only_account_reads_html_without_token_or_profile(self, get, post):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        get.return_value = html_response()
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url="https://pickup.example/show/credential/one@icloud.com",
            pickup_mode="independent_url",
        )

        code = client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0)

        self.assertEqual(code, "654321")
        called_url = get.call_args.args[0]
        self.assertIn("/show/credential/one@icloud.com", called_url)
        self.assertIn("n=10", called_url)
        self.assertEqual(get.call_args.kwargs["headers"]["Accept"], "text/html")
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])
        post.assert_not_called()

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_url_only_empty_page_then_new_html_message_returns_otp(self, get, sleep):
        empty = Mock(status_code=200, headers={})
        empty.text = (
            '<html><head><title>one@icloud.com</title></head><body>'
            '<div class="cnt">0 封</div><div class="no">等待接收邮件</div>'
            '</body></html>'
        )
        get.side_effect = [empty, html_response(subject="OpenAI code 222222", body="Code 222222")]
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url="https://pickup.example/show/credential/one@icloud.com",
            pickup_mode="independent_url",
        )

        code = client.fetch_latest_otp("one@icloud.com", AFTER_TS, 5, 1, 0)

        self.assertEqual(code, "222222")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    @patch("core.icloud_mail_client.requests.post")
    @patch("core.icloud_mail_client.requests.get")
    def test_mixed_mode_falls_back_from_html_to_same_mailbox_token(self, get, post):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        unavailable = Mock(status_code=500, headers={})
        unavailable.text = "upstream error"
        get.side_effect = [unavailable, response(subject="OpenAI code 333333")]
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="tok_one_1234",
            pickup_url="https://pickup.example/show/credential/one@icloud.com",
            pickup_mode="independent_url_with_token",
        )

        code = client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0)

        self.assertEqual(code, "333333")
        self.assertEqual(get.call_count, 2)
        self.assertIn("n=10", get.call_args_list[0].args[0])
        self.assertEqual(
            get.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer tok_one_1234",
        )
        post.assert_not_called()

    @patch("core.icloud_mail_client.requests.get")
    def test_two_url_mailboxes_keep_their_own_pickup_pages(self, get):
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url="https://pickup.example/show/first/one@icloud.com",
            pickup_mode="independent_url",
        )
        client._CONTEXT_CACHE["two@icloud.com"] = client.ICloudMailAccount(
            email="two@icloud.com",
            token="",
            pickup_url="https://pickup.example/show/second/two@icloud.com",
            pickup_mode="independent_url",
        )
        get.side_effect = [
            html_response(subject="OpenAI code 111111", body="Code 111111"),
            html_response(mailbox="two@icloud.com", subject="OpenAI code 222222", body="Code 222222"),
        ]

        self.assertEqual(client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0), "111111")
        self.assertEqual(client.fetch_latest_otp("two@icloud.com", AFTER_TS, 1, 1, 0), "222222")
        self.assertIn("/first/one@icloud.com", get.call_args_list[0].args[0])
        self.assertIn("/second/two@icloud.com", get.call_args_list[1].args[0])

    @patch("core.icloud_mail_client.requests.get")
    def test_url_network_error_hides_full_credential_url(self, get):
        pickup_url = "https://pickup.example/show/top-secret/one@icloud.com"
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url=pickup_url,
            pickup_mode="independent_url",
        )
        get.side_effect = requests.ConnectionError(f"network down for {pickup_url}?n=10")

        with self.assertRaises(client.ICloudMailError) as raised:
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

        self.assertNotIn("top-secret", str(raised.exception))
        self.assertNotIn(pickup_url, str(raised.exception))

    @patch("core.icloud_mail_client.requests.get")
    def test_url_network_error_hides_reencoded_query_credentials(self, get):
        pickup_url = (
            "https://pickup.example/show/one@icloud.com"
            "?token=query%20secret&mail=one%40icloud.com"
        )
        requested_url = (
            "https://pickup.example/show/one@icloud.com"
            "?token=query+secret&mail=one%40icloud.com&n=10"
        )
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url=pickup_url,
            pickup_mode="independent_url",
        )
        get.side_effect = requests.ConnectionError(f"network down for {requested_url}")

        with self.assertRaises(client.ICloudMailError) as raised:
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

        message = str(raised.exception)
        self.assertNotIn("query+secret", message)
        self.assertNotIn("query%20secret", message)
        self.assertNotIn("token=", message)

    @patch("core.icloud_mail_client.requests.get")
    def test_url_network_error_hides_relative_url_query_credentials(self, get):
        pickup_url = (
            "https://pickup.example/show/one@icloud.com"
            "?token=query%20secret&mail=one%40icloud.com"
        )
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url=pickup_url,
            pickup_mode="independent_url",
        )
        get.side_effect = requests.ConnectionError(
            "Max retries exceeded with url: "
            "/show/one@icloud.com?token=query+secret&mail=one%40icloud.com&n=10"
        )

        with self.assertRaises(client.ICloudMailError) as raised:
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

        message = str(raised.exception)
        self.assertNotIn("query+secret", message)
        self.assertNotIn("token=", message)
        self.assertNotIn("mail=", message)

    @patch("core.icloud_mail_client.requests.get")
    def test_html_page_for_another_mailbox_is_rejected(self, get):
        item = html_response()
        item.text = item.text.replace(
            "<html>",
            "<html><head><title>two@icloud.com</title></head>",
        )
        get.return_value = item
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="",
            pickup_url="https://pickup.example/show/credential/one@icloud.com",
            pickup_mode="independent_url",
        )

        with self.assertRaisesRegex(client.ICloudMailError, "页面邮箱不匹配"):
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

    def test_html_naive_timestamp_can_be_interpreted_as_china_time(self):
        china = timezone(timedelta(hours=8))
        expected = datetime(2026, 8, 2, 6, 0, 0, tzinfo=timezone.utc).timestamp()

        stamp = client._message_timestamp(
            "2026-08-02 14:00:00",
            assume_timezone=china,
        )

        self.assertEqual(stamp, expected)

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_ignores_browser_render_pickup_url(self, get):
        get.return_value = response(text="Your code is 654321")
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="tok_one_1234",
            pickup_url="https://flysms.xyz/icloud/pickup#mailbox=one%40icloud.com&token=secret",
        )

        self.assertEqual(client.fetch_latest_otp("one@icloud.com", AFTER_TS, 1, 1, 0), "654321")
        self.assertEqual(
            get.call_args.args[0],
            "https://flysms.top/icloud/api/pickup/messages/latest",
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

    def test_repeated_pickup_and_profile_5xx_fail_fast_as_provider_outage(self):
        client._email_cfg.ICLOUD_PROFILE_TOKEN = "profile_secret_1234"
        pickup_error = Mock(status_code=502, headers={})
        profile_error = Mock(status_code=500, headers={})

        with patch("core.icloud_mail_client.requests.get", return_value=pickup_error) as get, \
             patch("core.icloud_mail_client.requests.post", return_value=profile_error) as post, \
             patch("core.icloud_mail_client.time.sleep") as sleep, \
             patch("core.icloud_mail_client.time.monotonic", side_effect=[0, 0, 0, 1, 1, 2]):
            with self.assertRaisesRegex(
                client.ICloudProviderUnavailableError,
                "Pickup HTTP 502.*Profile HTTP 500",
            ):
                client.fetch_latest_otp(
                    "one@icloud.com",
                    after_ts=AFTER_TS,
                    max_wait=90,
                    poll_interval=3,
                    settle_seconds=0,
                )

        self.assertEqual(get.call_count, 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    @patch("core.icloud_mail_client.requests.get")
    def test_network_error_is_reported_without_exposing_token(self, get):
        get.side_effect = requests.ConnectionError("network down for tok_one_1234")

        with self.assertRaisesRegex(client.ICloudMailError, r"ConnectionError: network down for \*\*\*") as raised:
            client.fetch_latest_otp("one@icloud.com", AFTER_TS, 0, 1, 0)

        self.assertNotIn("tok_one_1234", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
