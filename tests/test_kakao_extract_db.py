import copy
import unittest
from unittest.mock import patch

from core import db


class KakaoExtractDbTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {
                "id": 1,
                "email": "one@example.com",
                "access_token": "TOKEN_ONE",
            }
        ]
        self.saved = []
        self.load_patch = patch.object(db, "_load_accounts", side_effect=lambda: self.accounts)
        self.save_patch = patch.object(
            db,
            "_save_accounts",
            side_effect=lambda rows: self.saved.append(copy.deepcopy(rows)),
        )
        self.load_patch.start()
        self.save_patch.start()

    def tearDown(self):
        self.load_patch.stop()
        self.save_patch.stop()

    def test_claim_persists_provider_and_result_mapping_metadata(self):
        claimed = db.claim_account_extract(
            1,
            trigger="manual_bulk",
            link_type="kakao_pay",
            provider="kakao_batch",
            batch_number=1,
            batch_total=3,
            result_index=0,
        )

        self.assertTrue(claimed)
        row = self.accounts[0]
        self.assertEqual(row["extract_link_provider"], "kakao_batch")
        self.assertEqual(row["extract_link_batch_number"], 1)
        self.assertEqual(row["extract_link_batch_total"], 3)
        self.assertEqual(row["extract_link_result_index"], 0)

    def test_update_persists_batch_result_metadata(self):
        db.claim_account_extract(1, provider="kakao_batch")

        updated = db.update_account_extract(1, {
            "ok": False,
            "status": "running",
            "provider": "kakao_batch",
            "batch_id": "batch-1",
            "batch_number": 1,
            "batch_total": 2,
            "result_index": 0,
            "charged_count": 1,
            "cdk_remaining": 9,
        })

        self.assertTrue(updated)
        row = self.accounts[0]
        self.assertEqual(row["extract_link_batch_id"], "batch-1")
        self.assertEqual(row["extract_link_charged_count"], 1)
        self.assertEqual(row["extract_link_cdk_remaining"], 9)

    def test_recovery_groups_kakao_batches_and_fails_unrecoverable_rows(self):
        self.accounts[:] = [
            {
                "id": 10,
                "extract_link_status": "running",
                "extract_link_provider": "kakao_batch",
                "extract_link_batch_id": "batch-1",
                "extract_link_batch_number": 1,
                "extract_link_batch_total": 2,
                "extract_link_result_index": 0,
            },
            {
                "id": 11,
                "extract_link_status": "queued",
                "extract_link_provider": "kakao_batch",
                "extract_link_batch_id": "batch-1",
                "extract_link_batch_number": 1,
                "extract_link_batch_total": 2,
                "extract_link_result_index": 1,
            },
            {
                "id": 12,
                "extract_link_status": "running",
                "extract_link_provider": "legacy",
            },
            {
                "id": 13,
                "extract_link_status": "queued",
                "extract_link_provider": "kakao_batch",
            },
        ]

        recovery = db.recover_interrupted_extract_links()

        self.assertEqual(recovery["failed_count"], 2)
        self.assertEqual(len(recovery["kakao_batches"]), 1)
        batch = recovery["kakao_batches"][0]
        self.assertEqual(batch["batch_id"], "batch-1")
        self.assertEqual(batch["accounts"], [
            {"account_id": 10, "result_index": 0},
            {"account_id": 11, "result_index": 1},
        ])
        self.assertEqual(self.accounts[0]["extract_link_status"], "queued")
        self.assertEqual(self.accounts[1]["extract_link_status"], "queued")
        self.assertEqual(self.accounts[2]["extract_link_status"], "failed")
        self.assertEqual(self.accounts[3]["extract_link_status"], "failed")

    def test_release_only_changes_queued_claims(self):
        self.accounts[:] = [
            {"id": 1, "extract_link_status": "queued"},
            {"id": 2, "extract_link_status": "running"},
        ]

        released = db.release_account_extract_claims([1, 2], error="队列提交失败")

        self.assertEqual(released, 1)
        self.assertEqual(self.accounts[0]["extract_link_status"], "failed")
        self.assertEqual(self.accounts[0]["extract_link_error"], "队列提交失败")
        self.assertEqual(self.accounts[1]["extract_link_status"], "running")


if __name__ == "__main__":
    unittest.main()
