# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class CloudflareWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_cloudflare_without_api_base(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudflare"
        ), patch.object(email_config, "CLOUDFLARE_API_BASE", "", create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Cloudflare API 地址", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_admin_mode_without_key(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudflare"
        ), patch.object(email_config, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDFLARE_AUTH_MODE", "x-admin-auth", create=True
        ), patch.object(email_config, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            email_config, "CLOUDFLARE_PATH_ACCOUNTS", "/admin/new_address", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Cloudflare API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_cloudflare_skips_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudflare"
        ), patch.object(email_config, "CLOUDFLARE_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDFLARE_AUTH_MODE", "none", create=True
        ), patch.object(email_config, "CLOUDFLARE_API_KEY", "", create=True), patch.object(
            email_config, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1, email_source="cloudflare")


if __name__ == "__main__":
    unittest.main()
