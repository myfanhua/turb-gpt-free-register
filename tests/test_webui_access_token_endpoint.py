# -*- coding: utf-8 -*-
"""账号 access token 按需读取接口与前端行为约束。"""
from pathlib import Path
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiAccessTokenEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_account_list_redacts_token_but_includes_presence_flag(self):
        row = {"id": 17, "email": "fixture@example.test", "access_token": "test-only-token", "totp_secret": "fixture-totp", "copy_line": "fixture@example.test----test-only-token", "password": "fixture-password", "codex_agent_token": "fixture-agent-token", "extra_json": "{}"}
        empty_row = {"id": 18, "email": "empty@example.test", "access_token": ""}
        with patch("webui.app.db.list_accounts", return_value=[row, empty_row]):
            response = self.client.get("/api/accounts", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = response.get_json()[0]
        self.assertNotIn("access_token", item)
        self.assertNotIn("copy_line", item)
        self.assertNotIn("password", item)
        self.assertNotIn("totp_secret", item)
        self.assertNotIn("codex_agent_token", item)
        self.assertTrue(item["has_access_token"])
        self.assertTrue(item["has_totp_secret"])
        self.assertFalse(response.get_json()[1]["has_access_token"])

    def test_access_token_endpoint_requires_webui_authorization(self):
        response = self.client.post("/api/accounts/17/access-token")
        self.assertEqual(response.status_code, 401)

    def test_access_token_endpoint_returns_only_requested_account_token(self):
        account = {"id": 17, "email": "fixture@example.test", "access_token": "test-only-token"}
        with patch("webui.app.db.get_account", return_value=account) as get_account:
            response = self.client.post("/api/accounts/17/access-token", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "access_token": "test-only-token"})
        get_account.assert_called_once_with(17)

    def test_access_token_endpoint_reports_missing_account_or_token(self):
        with patch("webui.app.db.get_account", return_value=None):
            missing = self.client.post("/api/accounts/17/access-token", headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertIn("账号不存在", missing.get_json()["error"])

        with patch("webui.app.db.get_account", return_value={"id": 17, "access_token": ""}):
            no_token = self.client.post("/api/accounts/17/access-token", headers=self.headers)
        self.assertEqual(no_token.status_code, 404)
        self.assertIn("没有 access_token", no_token.get_json()["error"])

    def test_email_asset_endpoint_is_authorized_and_preserves_provider_delivery_format(self):
        assurivo = {"id": 17, "email": "fixture@example.test", "extra_json": '{"email_asset":{"provider":"assurivo","email_address":"fixture@example.test","query_url":"https://assurivo.example.test/query"}}'}
        with patch("webui.app.db.get_account", return_value=assurivo):
            response = self.client.post("/api/accounts/17/email-asset", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["format"], "email----完整查询URL")
        self.assertEqual(response.get_json()["delivery_line"], "fixture@example.test----https://assurivo.example.test/query")

        outlook = {"id": 18, "email": "fixture@example.test", "email_source": "outlook", "original_email_line": "fixture@example.test----pw----client----refresh", "extra_json": "{}"}
        with patch("webui.app.db.get_account", return_value=outlook):
            response = self.client.post("/api/accounts/18/email-asset", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["format"], "email----password----clientId----refreshToken")
        self.assertEqual(response.get_json()["delivery_line"], "fixture@example.test----pw----client----refresh")

    def test_email_pool_list_redacts_material_and_explicit_endpoint_formats_generic_api(self):
        row = {"email": "fixture@example.test", "source": "generic_api", "code_url": "https://mail.example.test/code", "access_token": "fixture-token", "copy_line": "fixture@example.test----https://mail.example.test/code"}
        with patch("webui.app.db.list_generic_api_email_pool", return_value=[row]):
            response = self.client.get("/api/outlook?source=generic_api", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = response.get_json()[0]
        self.assertTrue(item["has_delivery_material"])
        self.assertNotIn("code_url", item)
        self.assertNotIn("access_token", item)
        self.assertNotIn("copy_line", item)

        with patch("webui.app.db.get_generic_api_email_by_email", return_value=row):
            material = self.client.post("/api/outlook/material", json={"source": "generic_api", "email": "fixture@example.test"}, headers=self.headers)
        self.assertEqual(material.status_code, 200)
        self.assertEqual(material.get_json()["format"], "email----取码地址")
        self.assertEqual(material.get_json()["delivery_line"], "fixture@example.test----https://mail.example.test/code")

    def test_refresh_session_endpoint_requires_auth_and_returns_redacted_result(self):
        unauthorized = self.client.post("/api/accounts/17/refresh-session")
        self.assertEqual(unauthorized.status_code, 401)
        result = {
            "ok": False,
            "refreshed": False,
            "reason": "browser_verification_required",
            "message": "需要真实浏览器验证",
            "http_status": 403,
            "status": {"expires_at": "2030-01-01T00:00:00Z", "refresh_label": "可尝试会话续期"},
        }
        with patch("webui.app.session_refresh.refresh_account_session", return_value=result):
            response = self.client.post("/api/accounts/17/refresh-session", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), result)
        self.assertNotIn("access_token", response.get_json())

    def test_export_response_includes_only_aggregate_token_status(self):
        account = {"id": 17, "email": "fixture@example.test", "access_token": "not-a-jwt", "extra_json": "{}"}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("core.account_exporters.Sub2APIExporter.export", return_value=(Path("sub2api-fixture.json"), {"accounts": 1, "missing": 3})):
            response = self.client.post("/api/accounts/export/sub2api", json={"account_ids": [17]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["token_summary"]["total"], 1)
        self.assertEqual(body["token_summary"]["oauth_refreshable"], 0)
        self.assertNotIn("access_token", body)

    def test_frontend_uses_explicit_endpoint_instead_of_list_tokens(self):
        html = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-copy-access-token", html)
        self.assertIn("/access-token", html)
        self.assertIn("copySelectedAccountAccessTokens", html)
        self.assertIn("data-refresh-session", html)
        self.assertIn("refreshAccountSession", html)
        self.assertIn("静态快照", html)
        self.assertIn("data-copy-email-asset", html)
        self.assertIn("data-account-details", html)
        self.assertIn("accountDrawer", html)
        self.assertIn("导出完整账号资产 JSON", html)
        self.assertIn("poolDrawer", html)
        self.assertIn("data-copy-pool-material", html)
        self.assertIn("导出选中邮箱素材", html)
        self.assertNotIn("ACCOUNTS.map(r=>r.access_token).filter(Boolean).join('\\n')", html)


if __name__ == "__main__":
    unittest.main()
