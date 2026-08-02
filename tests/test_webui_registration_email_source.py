# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class RegistrationEmailSourceWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_email_sources_returns_safe_catalog_and_configured_snapshot(self):
        with patch.object(email_config, "EMAIL_SOURCE", "icloud_api"):
            response = self.client.get("/api/email-sources")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["configured"], "icloud_api")
        self.assertEqual(payload["configured_label"], "iCloud 全部")
        self.assertIn(
            {"value": "icloud_url", "label": "iCloud 独立 URL"},
            payload["options"],
        )
        serialized = str(payload).lower()
        self.assertNotIn("pickup_url", serialized)
        self.assertNotIn("auth_token", serialized)

    @patch("webui.app.db.icloud_email_pool_summary", return_value={"available": 2, "total": 2})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}, {"id": 2}])
    def test_jobs_accepts_explicit_icloud_url_source(self, submit, summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "outlook"
        ):
            response = self.client.post("/api/jobs", json={
                "count": 2,
                "workers": 2,
                "email_source": "icloud_url",
            })

        self.assertEqual(response.status_code, 200)
        summary.assert_called_once_with(pickup_filter="url")
        submit.assert_called_once_with(count=2, workers=2, email_source="icloud_url")
        payload = response.get_json()
        self.assertEqual(payload["email_source"], "icloud_url")
        self.assertEqual(payload["email_source_label"], "iCloud 独立 URL")

    @patch("webui.app.db.icloud_email_pool_summary", return_value={"available": 1, "total": 1})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_icloud_virtual_sources_use_matching_pool_filters(self, submit, summary):
        for source, pickup_filter in (
            ("icloud_api", "all"),
            ("icloud_api_token", "token"),
            ("icloud_url", "url"),
        ):
            with self.subTest(source=source):
                submit.reset_mock()
                summary.reset_mock()
                response = self.client.post("/api/jobs", json={
                    "count": 1,
                    "workers": 1,
                    "email_source": source,
                })
                self.assertEqual(response.status_code, 200)
                summary.assert_called_once_with(pickup_filter=pickup_filter)
                submit.assert_called_once_with(count=1, workers=1, email_source=source)

    @patch("webui.app.db.icloud_email_pool_summary", return_value={"available": 1, "total": 1})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_empty_source_snapshots_current_config(self, submit, summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "icloud_api_token"
        ):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_source": "",
            })

        self.assertEqual(response.status_code, 200)
        summary.assert_called_once_with(pickup_filter="token")
        submit.assert_called_once_with(count=1, workers=1, email_source="icloud_api_token")

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_unknown_or_multi_source_selection(self, submit):
        for source in ("unknown", "outlook,icloud_url", "outlook|icloud_url"):
            with self.subTest(source=source):
                response = self.client.post("/api/jobs", json={
                    "count": 1,
                    "workers": 1,
                    "email_source": source,
                })
                self.assertEqual(response.status_code, 400)
        submit.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary", return_value={"available": 1, "total": 1})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_explicit_source_validation_ignores_unselected_global_provider(self, submit, outlook_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_source": "outlook",
            })

        self.assertEqual(response.status_code, 200)
        outlook_summary.assert_called_once_with()
        submit.assert_called_once_with(count=1, workers=1, email_source="outlook")


if __name__ == "__main__":
    unittest.main()
