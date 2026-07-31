# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import email_provider


class ICloudEmailProviderTests(unittest.TestCase):
    def test_parse_email_sources_accepts_icloud_api(self):
        self.assertEqual(
            email_provider.parse_email_sources("icloud_api,outlook,icloud_api"),
            ["icloud_api", "outlook"],
        )

    @patch("core.icloud_mail_client.pick_account")
    def test_acquire_email_uses_icloud_client(self, pick_account):
        pick_account.return_value.email = "one@icloud.com"
        with patch("core.email_provider.parse_email_sources", return_value=["icloud_api"]):
            self.assertEqual(email_provider.acquire_email(), "one@icloud.com")

    @patch("core.icloud_mail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="icloud_api")
    def test_wait_for_otp_routes_explicit_email(self, resolve, fetch):
        code = email_provider.wait_for_otp("one@icloud.com", after_ts=100, max_wait=10)
        self.assertEqual(code, "654321")
        fetch.assert_called_once_with("one@icloud.com", after_ts=100, max_wait=10)

    @patch("core.db.release_unconsumed_icloud_email", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="icloud_api")
    def test_release_unconsumed_routes_to_icloud_pool(self, resolve, release):
        self.assertTrue(email_provider.release_email_if_unconsumed("one@icloud.com", note="retry"))
        release.assert_called_once_with("one@icloud.com", note="retry")


if __name__ == "__main__":
    unittest.main()
