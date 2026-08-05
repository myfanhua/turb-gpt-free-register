# -*- coding: utf-8 -*-
import copy
import json
import unittest
from unittest.mock import patch

from core import db


class AccessTokenRecoveryDbTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"id": 1, "email": "missing@example.com", "access_token": ""},
            {"id": 2, "email": "ready@example.com", "access_token": "TOKEN_READY"},
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

    def test_claim_only_accepts_missing_token_account(self):
        claimed = db.claim_account_access_token_recovery(
            1, trigger="manual", log_file="C:/logs/one.log"
        )
        existing = db.claim_account_access_token_recovery(
            2, trigger="manual", log_file="C:/logs/two.log"
        )

        self.assertTrue(claimed["accepted"])
        self.assertEqual(self.accounts[0]["at_recovery_status"], "queued")
        self.assertTrue(existing["skipped"])
        self.assertEqual(self.accounts[1]["access_token"], "TOKEN_READY")

    def test_second_claim_is_busy(self):
        db.claim_account_access_token_recovery(1, trigger="manual", log_file="C:/logs/one.log")
        second = db.claim_account_access_token_recovery(
            1, trigger="manual_bulk", log_file="C:/logs/two.log"
        )

        self.assertFalse(second["accepted"])
        self.assertTrue(second["busy"])

    def test_success_writes_session_metadata_without_overwriting_existing_token(self):
        db.claim_account_access_token_recovery(1, trigger="manual", log_file="C:/logs/one.log")
        db.mark_account_access_token_recovery_running(1)
        result = db.complete_account_access_token_recovery(
            1,
            session_info={
                "accessToken": "TOKEN_NEW",
                "user": {"id": "user-1", "name": "Recovered User"},
                "account": {"id": "acct-1", "planType": "free"},
                "expires": "2026-08-06T00:00:00Z",
            },
            device_id="device-1",
            proxy_used="http://sid-1:bridge@127.0.0.1:25001",
        )

        self.assertTrue(result["updated"])
        row = self.accounts[0]
        self.assertEqual(row["access_token"], "TOKEN_NEW")
        self.assertEqual(row["user_id"], "user-1")
        self.assertEqual(row["user_name"], "Recovered User")
        self.assertEqual(row["plan_type"], "free")
        self.assertEqual(row["device_id"], "device-1")
        self.assertEqual(row["proxy_used"], "http://sid-1:bridge@127.0.0.1:25001")
        self.assertEqual(row["at_recovery_status"], "success")
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["account"]["id"], "acct-1")

        self.accounts[1]["at_recovery_status"] = "running"
        untouched = db.complete_account_access_token_recovery(
            2,
            session_info={"accessToken": "TOKEN_DIFFERENT"},
            device_id="device-2",
            proxy_used="http://proxy-2",
        )
        self.assertTrue(untouched["already_present"])
        self.assertEqual(self.accounts[1]["access_token"], "TOKEN_READY")

    def test_stop_and_restart_recovery_preserve_account_data(self):
        db.claim_account_access_token_recovery(1, trigger="manual", log_file="C:/logs/one.log")
        stopped = db.request_account_access_token_recovery_stop(1)
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.accounts[0]["at_recovery_status"], "stopped")

        self.accounts[0].update({
            "at_recovery_status": "running",
            "at_recovery_stop_requested": False,
        })
        recovered = db.recover_interrupted_access_token_recoveries()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.accounts[0]["at_recovery_status"], "failed")
        self.assertEqual(self.accounts[0]["access_token"], "")

    def test_lightweight_snapshot_includes_recovery_status_but_not_log_path(self):
        self.accounts[0].update({
            "at_recovery_status": "failed",
            "at_recovery_error": "OTP 超时",
            "at_recovery_log_file": "C:/private/recovery.log",
        })
        item = next(
            row for row in db.list_account_plan_check_statuses()["items"]
            if row["id"] == 1
        )
        self.assertEqual(item["at_recovery_status"], "failed")
        self.assertEqual(item["at_recovery_error"], "OTP 超时")
        self.assertTrue(item["has_at_recovery_log"])
        self.assertNotIn("at_recovery_log_file", item)


if __name__ == "__main__":
    unittest.main()
