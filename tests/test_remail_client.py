# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from config import email as email_config
from core import remail_client


class RemailClientTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()

    @patch("core.remail_client.requests.request")
    def test_pick_account_creates_code_order_and_caches_service_token(self, request):
        response = Mock(status_code=201)
        response.json.return_value = {
            "id": 3021,
            "orderNo": "R20260829123456789",
            "status": "active",
            "deliveryEmail": "fresh@outlook.test",
            "serviceToken": "st-test-token",
        }
        request.return_value = response

        with patch.object(email_config, "REMAIL_API_BASE", "https://remail.aishop6.com/docs", create=True), patch.object(
            email_config, "REMAIL_API_KEY", "rk-test-key", create=True
        ), patch.object(email_config, "REMAIL_PROJECT_ID", 1001, create=True), patch.object(
            email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True
        ), patch.object(email_config, "REMAIL_SERVICE_MODE", "code", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "private_first", create=True
        ):
            account = remail_client.pick_account()

        self.assertEqual(account.email, "fresh@outlook.test")
        self.assertEqual(account.service_token, "st-test-token")
        self.assertIs(remail_client.get_account_context("FRESH@OUTLOOK.TEST"), account)
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("POST", "https://remail.aishop6.com/v1/open/orders"))
        self.assertEqual(kwargs["params"], {"serviceMode": "code", "supply": "private_first"})
        self.assertEqual(kwargs["json"], {"projectId": 1001, "emailSuffix": "outlook.com"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer rk-test-key")
        self.assertTrue(kwargs["headers"]["Idempotency-Key"].startswith("turb-gpt-free-register-"))

    @patch("core.remail_client.time.sleep")
    @patch("core.remail_client.requests.request")
    def test_pick_account_can_wait_for_order_credentials(self, request, sleep):
        create_response = Mock(status_code=201)
        create_response.json.return_value = {
            "orderNo": "R20260829123456789",
            "status": "paid",
        }
        detail_response = Mock(status_code=200)
        detail_response.json.return_value = {
            "orderNo": "R20260829123456789",
            "status": "active",
            "deliveryEmail": "fresh@outlook.test",
            "serviceToken": "st-test-token",
        }
        request.side_effect = [create_response, detail_response]

        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SERVICE_MODE", "code", create=True
        ), patch.object(email_config, "REMAIL_ORDER_WAIT_SECONDS", 1, create=True):
            account = remail_client.pick_account()

        self.assertEqual(account.email, "fresh@outlook.test")
        self.assertEqual(
            request.call_args_list[1].args[:2],
            ("GET", "https://remail.aishop6.com/v1/open/orders/R20260829123456789"),
        )

    @patch("core.remail_client.requests.request")
    def test_fetch_latest_otp_uses_pickup_token_without_api_key(self, request):
        account = remail_client.RemailAccount(
            email="fresh@outlook.test",
            service_token="st-test-token",
            order_no="R1",
            project_id=1001,
            email_suffix="outlook.com",
        )
        remail_client._CONTEXT_CACHE["fresh@outlook.test"] = account
        response = Mock(status_code=200)
        response.json.return_value = {
            "items": [
                {
                    "id": 1,
                    "receivedAt": "2026-08-29T05:00:00Z",
                    "subject": "old code",
                    "verificationCode": "111111",
                },
                {
                    "id": 2,
                    "receivedAt": "2026-08-29T05:01:00Z",
                    "sender": "noreply@openai.com",
                    "subject": "Your verification code",
                    "bodyPreview": "Your code is 654321",
                    "verificationCode": "654321",
                },
            ],
            "fetch": {"lastStatus": "succeeded"},
        }
        request.return_value = response

        code = remail_client.fetch_latest_otp(
            "fresh@outlook.test",
            after_ts=remail_client._parse_timestamp("2026-08-29T05:00:30Z"),
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        kwargs = request.call_args.kwargs
        self.assertEqual(request.call_args.args[:2], ("GET", "https://remail.aishop6.com/v1/pickup"))
        self.assertEqual(kwargs["params"], {"email": "fresh@outlook.test", "token": "st-test-token"})
        self.assertNotIn("Authorization", kwargs["headers"])

    @patch("core.remail_client._saved_context_metadata")
    @patch("core.remail_client.requests.request")
    def test_fetch_latest_otp_restores_saved_service_token_after_restart(self, request, saved):
        saved.return_value = {
            "source": "remail",
            "email": "fresh@outlook.test",
            "service_token": "st-saved-token",
            "order_no": "R-SAVED",
            "project_id": 2,
            "email_suffix": "outlook.com",
        }
        response = Mock(status_code=200)
        response.json.return_value = {
            "items": [{
                "id": 2,
                "receivedAt": "2026-08-29T05:01:00Z",
                "subject": "Your verification code",
                "bodyPreview": "Your code is 654321",
                "verificationCode": "654321",
            }],
        }
        request.return_value = response

        code = remail_client.fetch_latest_otp(
            "FRESH@OUTLOOK.TEST",
            after_ts=remail_client._parse_timestamp("2026-08-29T05:00:30Z"),
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        self.assertEqual(
            request.call_args.args[:2],
            ("GET", "https://remail.aishop6.com/v1/pickup"),
        )
        self.assertEqual(
            request.call_args.kwargs["params"],
            {"email": "FRESH@OUTLOOK.TEST", "token": "st-saved-token"},
        )

    @patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True)
    @patch("core.remail_client._saved_context_metadata", return_value={})
    @patch("core.remail_client.requests.request")
    def test_restore_context_requires_exact_email_match(self, request, saved):
        response = Mock(status_code=200)
        response.json.return_value = {
            "items": [
                {
                    "orderNo": "R-WRONG",
                    "deliveryEmail": "other@outlook.test",
                    "serviceToken": "st-wrong",
                },
                {
                    "orderNo": "R-EXACT",
                    "deliveryEmail": "FRESH@OUTLOOK.TEST",
                    "serviceToken": "st-exact",
                },
            ],
        }
        request.return_value = response

        account = remail_client.restore_account_context("fresh@outlook.test")

        self.assertIsNotNone(account)
        self.assertEqual(account.email, "FRESH@OUTLOOK.TEST")
        self.assertEqual(account.service_token, "st-exact")
        self.assertEqual(request.call_args.kwargs["params"]["search"], "fresh@outlook.test")

    def test_pick_account_requires_project_id(self):
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 0, create=True
        ):
            with self.assertRaisesRegex(remail_client.RemailError, "项目 ID"):
                remail_client.pick_account()

    def test_release_account_clears_cached_context(self):
        remail_client._CONTEXT_CACHE["fresh@outlook.test"] = remail_client.RemailAccount(
            email="fresh@outlook.test",
            service_token="st-test-token",
            order_no="R1",
            project_id=1001,
            email_suffix="outlook.com",
        )
        remail_client.release_account("fresh@outlook.test", status="failed")
        self.assertIsNone(remail_client.get_account_context("fresh@outlook.test"))


if __name__ == "__main__":
    unittest.main()
