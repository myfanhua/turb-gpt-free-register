# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class GPTMailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_gptmail_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("请填写 GPTMail API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_gptmail_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.db.count_accounts", return_value=0)
    def test_summary_does_not_count_gptmail_as_outlook_pool(self, count_accounts, outlook_pool_summary, domain_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "EMAIL_SOURCE", "gptmail"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["outlook_total"], 0)
        outlook_pool_summary.assert_not_called()
