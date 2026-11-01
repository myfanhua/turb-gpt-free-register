# -*- coding: utf-8 -*-
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountRegistrationFilterTests(unittest.TestCase):
    def test_sql_and_python_match_registration_checkpoint(self):
        cases = [
            ({"registration_stage": "password_registered"}, True),
            (json.dumps({"registration_stage": "password_registered"}), True),
            ({"registration_stage": "session_ready"}, False),
            ({"registration_stage": "complete", "registration_password": "saved"}, False),
            ({}, False), (None, False), ("invalid json", False),
            ("[]", False), ("null", False), (42, False),
        ]
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE accounts (payload TEXT)")
            for extra, expected in cases:
                with self.subTest(extra=extra):
                    row = {"extra_json": extra, "codex_status": "missing"}
                    conn.execute("DELETE FROM accounts")
                    conn.execute("INSERT INTO accounts VALUES (?)", (json.dumps(row),))
                    where, params = db._account_filter_sql(codex_filter="password_registered")
                    result = conn.execute(
                        "SELECT COUNT(*) FROM accounts WHERE " + " AND ".join(where), params
                    ).fetchone()[0]
                    self.assertEqual(bool(result), expected)
                    self.assertEqual(db._matches_codex_status_filter(row, "password_registered"), expected)

    def test_pagination_and_snapshot_use_registration_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            rows = [
                {"id": i, "email": f"user{i}@example.com", "extra_json": json.dumps({
                    "registration_stage": "password_registered" if i < 3 else "session_ready"
                })}
                for i in range(1, 4)
            ]
            accounts_path.write_text(json.dumps(rows), encoding="utf-8")
            missing = root / "missing.json"
            with patch.multiple(
                db, _ACCOUNTS_JSON=accounts_path, _LEGACY_ACCOUNTS_JSON=missing,
                _OUTLOOK_JSON=missing, _GENERIC_API_EMAIL_JSON=missing,
                _JOBS_JSON=missing, _DOMAIN_EMAIL_JSON=missing,
            ), patch.object(db, "_SQLITE_READY", False), patch.object(db, "_SQLITE_READY_PATH", None):
                result = db.list_accounts_page(limit=1, codex_filter="password_registered")
                self.assertEqual(result["total"], 2)
                self.assertEqual([r["id"] for r in result["items"]], [2])
                result = db.list_accounts_page(limit=1, offset=1, codex_filter="password_registered")
                self.assertEqual([r["id"] for r in result["items"]], [1])
                snapshot = db.list_account_plan_check_statuses(codex_filter="password_registered")
                self.assertEqual(snapshot["total"], 2)
                self.assertEqual([r["id"] for r in snapshot["items"]], [2, 1])
                self.assertEqual(len(db.list_accounts(codex_filter="password_registered")), 2)


if __name__ == "__main__":
    unittest.main()
