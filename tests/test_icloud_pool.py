# -*- coding: utf-8 -*-
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core import db


class ICloudPoolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.patchers = [
            patch.object(db, "_ICLOUD_EMAIL_JSON", root / "icloud.json"),
            patch.object(db, "_ICLOUD_EMAIL_TXT", root / "icloud.txt"),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
            patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def test_import_inserts_updates_and_masks_tokens(self):
        first = db.import_icloud_emails([
            {"email": "One@icloud.com", "token": "tok_first_1234"},
            {"email": "two@icloud.com", "token": "tok_second_5678"},
        ])
        second = db.import_icloud_emails([
            {"email": "one@icloud.com", "token": "tok_new_9999"},
            {"email": "broken@icloud.com", "token": ""},
        ])

        self.assertEqual(first, {"inserted": 2, "updated": 0, "skipped": 0, "invalid": 0})
        self.assertEqual(second, {"inserted": 0, "updated": 1, "skipped": 0, "invalid": 1})
        rows = db.list_icloud_email_pool()
        one = next(row for row in rows if row["email"] == "one@icloud.com")
        self.assertNotIn("token", one)
        self.assertEqual(one["token_masked"], "tok_****9999")
        self.assertNotIn("tok_new_9999", str(rows))

    def test_import_preserves_pickup_url_and_returns_it_for_claim(self):
        pickup_url = "https://pickup.example/messages/latest?mail=one%40icloud.com"
        db.import_icloud_emails([
            {"email": "one@icloud.com", "token": "tok_one_1234", "pickup_url": pickup_url},
        ])

        claimed = db.claim_next_icloud_email()
        self.assertEqual(claimed["pickup_url"], pickup_url)
        self.assertEqual(claimed["pickup_mode"], "api_token")
        row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(row["pickup_url"], pickup_url)
        self.assertEqual(row["pickup_mode"], "api_token")

    def test_import_accepts_url_only_and_hides_raw_url_from_public_rows(self):
        pickup_url = "https://pickup.example/show/secret-value/one@icloud.com"

        result = db.import_icloud_emails([
            {"email": "one@icloud.com", "token": "", "pickup_url": pickup_url},
        ])

        self.assertEqual(result, {"inserted": 1, "updated": 0, "skipped": 0, "invalid": 0})
        public_row = db.get_icloud_email_by_email("one@icloud.com")
        self.assertEqual(public_row["pickup_mode"], "independent_url")
        self.assertTrue(public_row["has_pickup_url"])
        self.assertNotIn("pickup_url", public_row)
        self.assertNotIn("secret-value", str(public_row))

        claimed = db.claim_next_icloud_email()
        self.assertEqual(claimed["pickup_url"], pickup_url)
        self.assertEqual(claimed["pickup_mode"], "independent_url")

    def test_import_derives_api_and_mixed_pickup_modes(self):
        db.import_icloud_emails([
            {"email": "api@icloud.com", "token": "tok_api"},
            {
                "email": "mixed@icloud.com",
                "token": "tok_mixed",
                "pickup_url": "https://pickup.example/show/secret/mixed@icloud.com",
            },
        ])

        api_row = db.get_icloud_email_by_email("api@icloud.com")
        mixed_row = db.get_icloud_email_by_email("mixed@icloud.com")
        self.assertEqual(api_row["pickup_mode"], "api_token")
        self.assertFalse(api_row["has_pickup_url"])
        self.assertEqual(mixed_row["pickup_mode"], "independent_url_with_token")
        self.assertTrue(mixed_row["has_pickup_url"])

    def test_import_rejects_pickup_url_for_another_mailbox(self):
        result = db.import_icloud_emails([{
            "email": "one@icloud.com",
            "token": "",
            "pickup_url": "https://pickup.example/show/secret/two@icloud.com",
        }])

        self.assertEqual(result, {"inserted": 0, "updated": 0, "skipped": 0, "invalid": 1})
        self.assertEqual(db.icloud_email_pool_summary()["total"], 0)

    def test_import_updates_existing_mailbox_in_place(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_old"}])

        result = db.import_icloud_emails([{
            "email": "ONE@icloud.com",
            "token": "",
            "pickup_url": "https://pickup.example/show/secret/one@icloud.com",
        }])

        self.assertEqual(result, {"inserted": 0, "updated": 1, "skipped": 0, "invalid": 0})
        self.assertEqual(db.icloud_email_pool_summary()["total"], 1)
        claimed = db.claim_next_icloud_email()
        self.assertEqual(claimed["token"], "")
        self.assertEqual(claimed["pickup_mode"], "independent_url")

    def test_legacy_camel_case_pickup_url_is_normalized_and_hidden(self):
        pickup_url = "https://pickup.example/show/legacy-secret/one@icloud.com"
        db._ICLOUD_EMAIL_JSON.write_text(
            '[{"id":1,"email":"one@icloud.com","token":"","pickupUrl":"'
            + pickup_url
            + '","status":"available"}]',
            encoding="utf-8",
        )

        public_row = db.get_icloud_email_by_email("one@icloud.com")
        self.assertEqual(public_row["pickup_mode"], "independent_url")
        self.assertTrue(public_row["has_pickup_url"])
        self.assertNotIn("pickupUrl", public_row)
        self.assertNotIn("pickup_url", public_row)
        self.assertNotIn("legacy-secret", str(public_row))

        claimed = db.claim_next_icloud_email()
        self.assertEqual(claimed["pickup_url"], pickup_url)
        self.assertNotIn("pickupUrl", claimed)

    def test_short_tokens_are_never_echoed_in_full(self):
        for token in ("a", "abcd", "tok_", "tok_a"):
            with self.subTest(token=token):
                masked = db._mask_icloud_token(token)
                self.assertNotEqual(masked, token)
                self.assertNotIn(token, masked)

    def test_concurrent_claims_return_unique_mailboxes(self):
        db.import_icloud_emails([
            {"email": f"mail{i}@icloud.com", "token": f"tok_{i:04d}"}
            for i in range(8)
        ])

        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda _: db.claim_next_icloud_email(), range(8)))

        emails = [row["email"] for row in claimed]
        self.assertEqual(len(set(emails)), 8)
        self.assertEqual(db.icloud_email_pool_summary()["used"], 8)

    def test_token_filter_skips_url_only_mailboxes(self):
        db.import_icloud_emails([
            {
                "email": "url@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/secret/url@icloud.com",
            },
            {"email": "token@icloud.com", "token": "tok_only"},
        ])

        claimed = db.claim_next_icloud_email(pickup_filter="token")

        self.assertEqual(claimed["email"], "token@icloud.com")
        self.assertEqual(claimed["claimed_pickup_mode"], "api_token")

    def test_url_filter_skips_token_only_mailboxes(self):
        db.import_icloud_emails([
            {"email": "token@icloud.com", "token": "tok_only"},
            {
                "email": "url@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/secret/url@icloud.com",
            },
        ])

        claimed = db.claim_next_icloud_email(pickup_filter="url")

        self.assertEqual(claimed["email"], "url@icloud.com")
        self.assertEqual(claimed["claimed_pickup_mode"], "independent_url")

    def test_url_filter_skips_json_pickup_endpoints(self):
        db.import_icloud_emails([
            {
                "email": "json@icloud.com",
                "token": "tok_json",
                "pickup_url": "https://pickup.example/api/mail/pickup",
            },
            {
                "email": "latest@icloud.com",
                "token": "tok_latest",
                "pickup_url": "https://pickup.example/messages/latest?mail=latest%40icloud.com",
            },
            {
                "email": "page@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/secret/page@icloud.com",
            },
        ])

        claimed = db.claim_next_icloud_email(pickup_filter="url")

        self.assertEqual(claimed["email"], "page@icloud.com")
        self.assertEqual(db.icloud_email_pool_summary(pickup_filter="url")["total"], 1)

    def test_url_filter_skips_browser_render_urls_with_fragment_credentials(self):
        db.import_icloud_emails([
            {
                "email": "browser@icloud.com",
                "token": "tok_browser",
                "pickup_url": (
                    "https://flysms.xyz/icloud/pickup"
                    "#email=browser%40icloud.com&key=tok_browser"
                ),
            },
            {
                "email": "page@icloud.com",
                "token": "",
                "pickup_url": "https://icloud-api.top/show/secret/page@icloud.com",
            },
        ])

        claimed = db.claim_next_icloud_email(pickup_filter="url")

        self.assertEqual(claimed["email"], "page@icloud.com")
        self.assertEqual(db.icloud_email_pool_summary(pickup_filter="url")["total"], 1)

    def test_browser_render_url_with_token_is_classified_as_api_only(self):
        db.import_icloud_emails([{
            "email": "browser@icloud.com",
            "token": "tok_browser",
            "pickup_url": (
                "https://flysms.xyz/icloud/pickup"
                "#email=browser%40icloud.com&key=tok_browser"
            ),
        }])

        row = db.get_icloud_email_by_email("browser@icloud.com")

        self.assertEqual(row["pickup_mode"], "api_token")
        self.assertTrue(row["has_pickup_url"])

    def test_mixed_mailbox_can_be_forced_to_either_channel(self):
        material = {
            "email": "mixed@icloud.com",
            "token": "tok_mixed",
            "pickup_url": "https://pickup.example/show/secret/mixed@icloud.com",
        }
        db.import_icloud_emails([material])

        token_claim = db.claim_next_icloud_email(pickup_filter="token")
        self.assertEqual(token_claim["claimed_pickup_mode"], "api_token")
        db.release_icloud_email("mixed@icloud.com", status="available")

        url_claim = db.claim_next_icloud_email(pickup_filter="url")
        self.assertEqual(url_claim["claimed_pickup_mode"], "independent_url")

    def test_release_clears_claimed_pickup_mode(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one"}])
        db.claim_next_icloud_email(pickup_filter="token")

        db.release_icloud_email("one@icloud.com", status="available")

        row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertNotIn("claimed_pickup_mode", row)

    def test_released_available_mailbox_moves_to_claim_queue_tail(self):
        db.import_icloud_emails([
            {
                "email": "first@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/secret/first@icloud.com",
            },
            {
                "email": "second@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/secret/second@icloud.com",
            },
        ])
        first = db.claim_next_icloud_email(pickup_filter="url")
        self.assertEqual(first["email"], "first@icloud.com")

        db.release_icloud_email("first@icloud.com", status="available", note="stopped")
        next_claim = db.claim_next_icloud_email(pickup_filter="url")

        self.assertEqual(next_claim["email"], "second@icloud.com")

    def test_unconsumed_mailbox_moves_to_claim_queue_tail(self):
        db.import_icloud_emails([
            {"email": "first@icloud.com", "token": "tok_first"},
            {"email": "second@icloud.com", "token": "tok_second"},
        ])
        first = db.claim_next_icloud_email(pickup_filter="token")
        self.assertEqual(first["email"], "first@icloud.com")

        self.assertTrue(db.release_unconsumed_icloud_email("first@icloud.com", note="retry"))
        next_claim = db.claim_next_icloud_email(pickup_filter="token")

        self.assertEqual(next_claim["email"], "second@icloud.com")

    def test_filtered_summary_counts_only_eligible_available_mailboxes(self):
        db.import_icloud_emails([
            {"email": "token@icloud.com", "token": "tok_only"},
            {
                "email": "url@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/secret/url@icloud.com",
            },
            {
                "email": "mixed@icloud.com",
                "token": "tok_mixed",
                "pickup_url": "https://pickup.example/show/secret/mixed@icloud.com",
            },
        ])

        self.assertEqual(db.icloud_email_pool_summary(pickup_filter="token")["available"], 2)
        self.assertEqual(db.icloud_email_pool_summary(pickup_filter="url")["available"], 2)
        self.assertEqual(db.icloud_email_pool_summary(pickup_filter="all")["available"], 3)

    def test_concurrent_token_and_url_filters_do_not_double_claim_mixed_mailbox(self):
        db.import_icloud_emails([{
            "email": "mixed@icloud.com",
            "token": "tok_mixed",
            "pickup_url": "https://pickup.example/show/secret/mixed@icloud.com",
        }])

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(db.claim_next_icloud_email, ["token", "url"]))

        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_legacy_camel_case_url_is_eligible_for_url_filter(self):
        pickup_url = "https://pickup.example/show/legacy/one@icloud.com"
        db._ICLOUD_EMAIL_JSON.write_text(
            '[{"id":1,"email":"one@icloud.com","token":"","pickupUrl":"'
            + pickup_url
            + '","status":"available"}]',
            encoding="utf-8",
        )

        claimed = db.claim_next_icloud_email(pickup_filter="url")

        self.assertEqual(claimed["email"], "one@icloud.com")
        self.assertEqual(claimed["pickup_url"], pickup_url)

    def test_unconsumed_used_mailbox_returns_to_available(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()
        self.assertTrue(db.release_unconsumed_icloud_email("one@icloud.com", note="retry"))
        row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(row["status"], "available")
        self.assertIsNone(row["used_at"])

    def test_used_mailbox_cannot_be_deleted(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()
        self.assertFalse(db.delete_icloud_email("one@icloud.com"))

    def test_insert_account_marks_icloud_mailbox_registered_without_copying_token(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()
        db.insert_account(email="one@icloud.com", access_token="access-123", email_source="icloud_api")
        pool_row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(pool_row["status"], "registered")
        account = db.get_account_by_email("one@icloud.com")
        self.assertNotIn("tok_one_1234", str(account))

    def test_insert_account_does_not_consume_unclaimed_icloud_mailbox(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])

        db.insert_account(email="one@icloud.com", access_token="access-123", email_source="icloud_api")

        pool_row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(pool_row["status"], "available")

    def test_insert_account_from_another_source_does_not_consume_claimed_icloud_mailbox(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()

        db.insert_account(email="one@icloud.com", access_token="access-123", email_source="outlook")

        pool_row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(pool_row["status"], "used")

    def test_claim_returns_context_when_derived_txt_sync_fails(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])

        with patch.object(db, "_sync_icloud_email_txt", side_effect=OSError("txt locked")):
            claimed = db.claim_next_icloud_email()

        self.assertEqual(claimed["email"], "one@icloud.com")
        self.assertEqual(claimed["token"], "tok_one_1234")
        row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(row["status"], "used")


if __name__ == "__main__":
    unittest.main()
