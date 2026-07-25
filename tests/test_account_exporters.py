import json
import tempfile
import unittest
from pathlib import Path

from core.account_exporters import CockpitExporter, FullAccountAssetExporter, Sub2APIExporter, build_auth_artifacts, normalize_account


FAKE_JWT = "eyJhbGciOiJub25lIn0.eyJleHAiOjE3MDAwMDAwMDB9."


class AccountExporterTests(unittest.TestCase):
    def setUp(self):
        self.row = {"id": 7, "email": "u@example.com", "access_token": FAKE_JWT, "note": "hello", "extra_json": json.dumps({"auth_artifacts": {"registration_password": "pw", "session": {"user": "x"}}})}

    def test_normalizes_top_level_and_artifacts_without_fabricating_tokens(self):
        item = normalize_account(self.row)
        self.assertEqual(item["expires_at"], 1700000000)
        self.assertEqual(item["registration_password"], "pw")
        self.assertEqual(item["refresh_token"], "")
        self.assertEqual(item["id_token"], "")
        self.assertIn("oauth_refresh_token", item["missing"])

    def test_sub2api_document_and_cockpit_single_batch(self):
        sub2 = Sub2APIExporter().build([self.row])
        self.assertEqual(sub2["proxies"], [])
        self.assertEqual(sub2["accounts"][0]["platform"], "openai")
        self.assertEqual(sub2["accounts"][0]["credentials"]["access_token"], FAKE_JWT)
        cockpit = CockpitExporter().build([self.row])
        self.assertIsInstance(cockpit, list)
        self.assertEqual(CockpitExporter().build([self.row], single=True)["type"], "codex")
        self.assertEqual(cockpit[0]["refresh_token"], "")
        self.assertFalse(sub2["accounts"][0]["extra"]["refreshable"])
        self.assertTrue(sub2["accounts"][0]["extra"]["snapshot_only"])
        self.assertFalse(cockpit[0]["refreshable"])
        self.assertEqual(cockpit[0]["expires_at"], 1700000000)

    def test_outlook_refresh_token_is_not_exported_as_openai_oauth_refresh(self):
        row = dict(self.row, refresh_token="outlook-mail-refresh-token")
        item = normalize_account(row)
        self.assertEqual(item["refresh_token"], "")
        self.assertFalse(item["refreshable"])
        self.assertIn("oauth_refresh_token", item["missing"])

    def test_export_is_safe_and_does_not_mutate_source_or_log_token(self):
        before = json.dumps(self.row, sort_keys=True)
        with tempfile.TemporaryDirectory() as tmp:
            path, summary = Sub2APIExporter(Path(tmp)).export([self.row])
            self.assertTrue(path.parent == Path(tmp))
            self.assertTrue(path.name.startswith("sub2api-"))
            self.assertEqual(summary["accounts"], 1)
            self.assertEqual(json.loads(path.read_text())["accounts"][0]["credentials"]["access_token"], FAKE_JWT)
        self.assertEqual(json.dumps(self.row, sort_keys=True), before)

    def test_artifact_helper_keeps_only_real_supplied_values(self):
        self.assertEqual(build_auth_artifacts("a", registration_password=None, session_data=None, cookies=None), {"access_token": "a"})
        artifacts = build_auth_artifacts("a", registration_password="p", session_data={"x": 1}, cookies=[{"name": "x"}])
        self.assertEqual(artifacts["registration_password"], "p")
        self.assertEqual(artifacts["cookies"][0]["name"], "x")

    def test_full_asset_export_keeps_real_email_asset_and_missing_fields(self):
        row = dict(self.row)
        row["extra_json"] = json.dumps({"auth_artifacts": {"cookies": {"sid": "x"}}, "email_asset": {"provider": "assurivo", "email_address": "u@example.com", "email_credential": "secret", "query_url": "https://assurivo.com/console/open.php?mail=u%40example.com&pwd=secret&limit=5"}})
        before = json.dumps(row, sort_keys=True)
        with tempfile.TemporaryDirectory() as tmp:
            path, summary = FullAccountAssetExporter(Path(tmp)).export([row])
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name.split("-")[0], "full")
        self.assertEqual(summary["accounts"], 1)
        self.assertEqual(payload["accounts"][0]["email_asset"]["provider"], "assurivo")
        self.assertIn("oauth_refresh_token", payload["accounts"][0]["missing_fields"])
        self.assertEqual(json.dumps(row, sort_keys=True), before)

    def test_full_asset_export_old_account_marks_missing_asset(self):
        payload = FullAccountAssetExporter().build([self.row])
        self.assertIn("email_asset", payload["accounts"][0]["missing_fields"])


if __name__ == "__main__": unittest.main()
