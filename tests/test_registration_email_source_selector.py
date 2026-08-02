# -*- coding: utf-8 -*-
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from core import email_provider


class RegistrationEmailSourceSelectorTests(unittest.TestCase):
    def test_source_options_include_distinct_icloud_modes(self):
        options = email_provider.registration_source_options()
        by_value = {item["value"]: item["label"] for item in options}

        self.assertEqual(by_value["icloud_api"], "iCloud 全部")
        self.assertEqual(by_value["icloud_api_token"], "iCloud API")
        self.assertEqual(by_value["icloud_url"], "iCloud 独立 URL")
        self.assertNotIn("URL + API 后备", by_value.values())

    def test_parse_sources_accepts_virtual_icloud_selectors(self):
        self.assertEqual(
            email_provider.parse_email_sources("icloud_api_token,icloud_url"),
            ["icloud_api_token", "icloud_url"],
        )

    @patch("core.email_provider._pick_from_source", return_value="one@icloud.com")
    def test_acquire_email_uses_explicit_source_instead_of_global_config(self, pick):
        with patch("config.email.EMAIL_SOURCE", "outlook"):
            email = email_provider.acquire_email("icloud_url")

        self.assertEqual(email, "one@icloud.com")
        pick.assert_called_once_with("icloud_url")

    @patch("core.email_provider._pick_from_source")
    def test_acquire_email_without_argument_keeps_configured_fallback_order(self, pick):
        pick.side_effect = [RuntimeError("empty"), "second@example.com"]
        with patch("config.email.EMAIL_SOURCE", "outlook,generic_api"):
            email = email_provider.acquire_email()

        self.assertEqual(email, "second@example.com")
        self.assertEqual(
            [call.args[0] for call in pick.call_args_list],
            ["outlook", "generic_api"],
        )

    def test_canonical_source_maps_virtual_icloud_modes_to_icloud_api(self):
        self.assertEqual(email_provider.canonical_email_source("icloud_api_token"), "icloud_api")
        self.assertEqual(email_provider.canonical_email_source("icloud_url"), "icloud_api")
        self.assertEqual(email_provider.canonical_email_source("outlook"), "outlook")

    def test_snapshot_source_uses_current_config_when_selection_is_empty(self):
        with patch("config.email.EMAIL_SOURCE", "icloud_api,outlook"):
            self.assertEqual(
                email_provider.snapshot_registration_source(""),
                "icloud_api,outlook",
            )

    @patch("core.icloud_mail_client.pick_account")
    def test_virtual_icloud_selectors_choose_requested_claim_filter(self, pick):
        pick.return_value.email = "one@icloud.com"

        self.assertEqual(email_provider._pick_from_source("icloud_api_token"), "one@icloud.com")
        pick.assert_called_once_with(selection="token")
        pick.reset_mock()

        self.assertEqual(email_provider._pick_from_source("icloud_url"), "one@icloud.com")
        pick.assert_called_once_with(selection="url")

    @patch("core.icloud_mail_client.release_account")
    @patch("core.outlook_client.release_account")
    @patch("core.icloud_mail_client.fetch_latest_otp", return_value="111111")
    @patch("core.outlook_client.fetch_latest_otp", return_value="654321")
    @patch("core.db.get_icloud_email_by_email", return_value={"email": "duplicate@example.com"})
    @patch("core.email_provider._pick_from_source", return_value="duplicate@example.com")
    def test_acquired_source_routes_duplicate_email_through_selected_provider(
        self,
        pick,
        get_icloud,
        outlook_fetch,
        icloud_fetch,
        outlook_release,
        icloud_release,
    ):
        email = email_provider.acquire_email("outlook")

        code = email_provider.wait_for_otp(email, after_ts=100)
        released_source = email_provider.release_email(email, status="available")

        self.assertEqual(code, "654321")
        self.assertEqual(released_source, "outlook")
        outlook_fetch.assert_called_once_with(email, after_ts=100)
        icloud_fetch.assert_not_called()
        outlook_release.assert_called_once_with(email, status="available", note=None)
        icloud_release.assert_not_called()

    @patch("core.db.get_icloud_email_by_email", return_value={"email": "duplicate@example.com"})
    @patch("core.email_provider._pick_from_source", return_value="duplicate@example.com")
    def test_concurrent_acquisitions_keep_thread_local_source_for_duplicate_email(self, pick, get_icloud):
        barrier = Barrier(2)

        def acquire_and_resolve(source):
            email = email_provider.acquire_email(source)
            barrier.wait(timeout=2)
            return email_provider.resolve_email_source(email)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(acquire_and_resolve, ["outlook", "icloud_url"]))

        self.assertEqual(results, ["outlook", "icloud_api"])


if __name__ == "__main__":
    unittest.main()
