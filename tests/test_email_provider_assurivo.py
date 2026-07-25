# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider


class AssurivoProviderTests(unittest.TestCase):
    def test_source_list_keeps_assurivo_and_order(self):
        self.assertEqual(email_provider.parse_email_sources("outlook,assurivo"), ["outlook", "assurivo"])

    @patch("core.assurivo_mail_client.pick_account")
    def test_fallback_uses_assurivo_after_outlook_failure(self, pick):
        pick.return_value.email = "a@example.com"
        with patch("core.email_provider.parse_email_sources", return_value=["outlook", "assurivo"]), patch("core.outlook_client.pick_account", side_effect=RuntimeError("empty")):
            self.assertEqual(email_provider.acquire_email(), "a@example.com")

    @patch("core.assurivo_mail_client.fetch_latest_otp", return_value="123456")
    @patch("core.email_provider.resolve_email_source", return_value="assurivo")
    def test_dispatches_otp_to_assurivo(self, resolve, fetch):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("a@example.com", after_ts=1), "123456")
        fetch.assert_called_once_with("a@example.com", after_ts=1)

    @patch("core.assurivo_mail_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="assurivo")
    def test_release_dispatches_to_assurivo(self, resolve, release):
        self.assertEqual(email_provider.release_email("a@example.com", status="failed"), "assurivo")
        release.assert_called_once_with("a@example.com", status="failed", note=None)


if __name__ == "__main__":
    unittest.main()
