# -*- coding: utf-8 -*-
"""core.synthetic_auth 单元测试：codex-auth-helper 原理的本地实现。"""
from __future__ import annotations

import base64
import json
import time
import unittest

from core import synthetic_auth


def _b64d(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _fake_access_token(account_id="acc-123", plan_type="free", user_id="user-abc", exp=None) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": int(time.time()) - 100,
        "exp": exp or (int(time.time()) + 3600),
        "email": "someone@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan_type,
            "chatgpt_user_id": user_id,
            "user_id": user_id,
        },
    }
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc(header)}.{enc(payload)}.fakesig"


class SyntheticIdTokenTest(unittest.TestCase):
    def test_structure_matches_codex_auth_helper(self):
        token = synthetic_auth.build_synthetic_id_token(
            account_id="acc-1", plan_type="plus", user_id="user-1", email="a@b.com", exp=2000000000, iat=100
        )
        head, body, sig = token.split(".")
        self.assertEqual(sig, "synthetic")
        self.assertEqual(_b64d(head), {"alg": "none", "typ": "JWT", "cpa_synthetic": True})
        payload = _b64d(body)
        self.assertEqual(payload["iat"], 100)
        self.assertEqual(payload["exp"], 2000000000)
        self.assertEqual(payload["email"], "a@b.com")
        claim = payload["https://api.openai.com/auth"]
        self.assertEqual(claim["chatgpt_account_id"], "acc-1")
        self.assertEqual(claim["chatgpt_plan_type"], "plus")
        self.assertEqual(claim["chatgpt_user_id"], "user-1")
        self.assertEqual(claim["user_id"], "user-1")

    def test_no_email_omits_email_claims(self):
        token = synthetic_auth.build_synthetic_id_token(account_id="acc-1")
        payload = _b64d(token.split(".")[1])
        self.assertNotIn("email", payload)
        self.assertNotIn("email_verified", payload)


class ConvertAccountRowTest(unittest.TestCase):
    def _row(self, **kw):
        row = {
            "id": 1,
            "email": "someone@example.com",
            "access_token": _fake_access_token(**kw),
            "extra_json": "{}",
        }
        return row

    def test_convert_from_access_token_claims(self):
        converted = synthetic_auth.convert_account_row(self._row(account_id="acc-xyz", plan_type="plus"))
        self.assertEqual(converted["account_id"], "acc-xyz")
        self.assertEqual(converted["plan_type"], "plus")
        self.assertEqual(converted["email"], "someone@example.com")
        auth = converted["auth_json"]
        self.assertEqual(auth["auth_mode"], "chatgpt")
        self.assertIsNone(auth["OPENAI_API_KEY"])
        self.assertEqual(auth["tokens"]["account_id"], "acc-xyz")
        self.assertEqual(auth["tokens"]["refresh_token"], synthetic_auth.REFRESH_TOKEN_PLACEHOLDER)
        self.assertTrue(auth["last_refresh"].endswith("Z"))

    def test_session_token_used_as_refresh(self):
        row = self._row()
        row["extra_json"] = json.dumps({"auth_artifacts": {"session": {"sessionToken": "sess-999"}}})
        converted = synthetic_auth.convert_account_row(row)
        self.assertEqual(converted["auth_json"]["tokens"]["refresh_token"], "sess-999")
        self.assertEqual(converted["session_token"], "sess-999")

    def test_missing_access_token_raises(self):
        with self.assertRaises(ValueError):
            synthetic_auth.convert_account_row({"id": 1, "email": "a@b.com", "access_token": "", "extra_json": "{}"})

    def test_missing_account_id_raises(self):
        token = _fake_access_token(account_id="")
        with self.assertRaises(ValueError):
            synthetic_auth.convert_account_row({"id": 1, "email": "a@b.com", "access_token": token, "extra_json": "{}"})


class ExportEntryTest(unittest.TestCase):
    def _converted(self):
        return synthetic_auth.convert_account_row({
            "id": 1,
            "email": "a@b.com",
            "access_token": _fake_access_token(),
            "extra_json": "{}",
        })

    def test_sub2api_entry(self):
        entry = synthetic_auth.build_sub2api_session_entry(self._converted())
        self.assertEqual(entry["platform"], "openai")
        self.assertEqual(entry["type"], "oauth")
        self.assertTrue(entry["credentials"]["access_token"])
        self.assertTrue(entry["credentials"]["id_token"])
        self.assertTrue(entry["extra"]["synthetic_id_token"])
        self.assertFalse(entry["extra"]["refreshable"])

    def test_cockpit_entry(self):
        entry = synthetic_auth.build_cockpit_entry(self._converted())
        self.assertEqual(entry["type"], "codex")
        self.assertEqual(entry["email"], "a@b.com")
        self.assertTrue(entry["synthetic_id_token"])

    def test_write_files(self):
        import tempfile
        converted = self._converted()
        with tempfile.TemporaryDirectory() as td:
            p1 = synthetic_auth.write_auth_file(converted, export_dir=td)
            self.assertTrue(p1.is_file())
            data = json.loads(p1.read_text(encoding="utf-8"))
            self.assertEqual(data["auth_mode"], "chatgpt")
            p2 = synthetic_auth.write_sub2api_file([synthetic_auth.build_sub2api_session_entry(converted)], export_dir=td)
            self.assertEqual(len(json.loads(p2.read_text(encoding="utf-8"))["accounts"]), 1)
            p3 = synthetic_auth.write_cockpit_file([synthetic_auth.build_cockpit_entry(converted)], export_dir=td)
            self.assertEqual(len(json.loads(p3.read_text(encoding="utf-8"))), 1)


if __name__ == "__main__":
    unittest.main()
