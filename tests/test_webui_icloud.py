# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class ICloudWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.import_icloud_emails")
    def test_import_accepts_email_token_lines(self, import_icloud):
        import_icloud.return_value = {"inserted": 1, "updated": 1, "skipped": 0, "invalid": 1}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_api",
            "text": "one@icloud.com----tok_one\ntwo@icloud.com====tok_two\nbroken",
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["updated"], 1)
        self.assertEqual(payload["invalid"], 1)
        import_icloud.assert_called_once_with([
            {"email": "one@icloud.com", "token": "tok_one"},
            {"email": "two@icloud.com", "token": "tok_two"},
            {"email": "broken", "token": ""},
        ])

    @patch("webui.app.db.import_icloud_emails")
    def test_import_accepts_common_copied_separators(self, import_icloud):
        import_icloud.return_value = {"inserted": 4, "updated": 0, "skipped": 0, "invalid": 0}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_api",
            "text": (
                "tab@icloud.com\ttok_tab\n"
                "pipe@icloud.com|tok_pipe\n"
                "comma@icloud.com,tok_comma\n"
                "space@icloud.com tok_space"
            ),
        })
        self.assertEqual(response.status_code, 200)
        import_icloud.assert_called_once_with([
            {"email": "tab@icloud.com", "token": "tok_tab"},
            {"email": "pipe@icloud.com", "token": "tok_pipe"},
            {"email": "comma@icloud.com", "token": "tok_comma"},
            {"email": "space@icloud.com", "token": "tok_space"},
        ])

    @patch("webui.app.db.import_icloud_emails")
    def test_import_accepts_pickup_export_with_triple_dash_and_url(self, import_icloud):
        import_icloud.return_value = {"inserted": 1, "updated": 0, "skipped": 0, "invalid": 0}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_api",
            "text": "one@icloud.com---tok_one---https://pickup.example/messages?mail=one%40icloud.com",
        })
        self.assertEqual(response.status_code, 200)
        import_icloud.assert_called_once_with([
            {
                "email": "one@icloud.com",
                "token": "tok_one",
                "pickup_url": "https://pickup.example/messages?mail=one%40icloud.com",
            },
        ])

    @patch("webui.app.db.import_icloud_emails")
    def test_import_accepts_url_only_with_three_or_more_dashes(self, import_icloud):
        import_icloud.return_value = {"inserted": 5, "updated": 0, "skipped": 0, "invalid": 0}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_api",
            "text": (
                "dash-name@icloud.com---https://pickup.example/show/cred-with-dash/dash-name@icloud.com\n"
                "four@icloud.com----https://pickup.example/show/credential/four@icloud.com\n"
                "five@icloud.com-----https://pickup.example/show/credential/five@icloud.com\n"
                "six@icloud.com------https://pickup.example/show/credential/six@icloud.com\n"
                "mixed@icloud.com----tok_mixed----https://pickup.example/show/credential/mixed@icloud.com"
            ),
        })

        self.assertEqual(response.status_code, 200)
        import_icloud.assert_called_once_with([
            {
                "email": "dash-name@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/cred-with-dash/dash-name@icloud.com",
            },
            {
                "email": "four@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/credential/four@icloud.com",
            },
            {
                "email": "five@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/credential/five@icloud.com",
            },
            {
                "email": "six@icloud.com",
                "token": "",
                "pickup_url": "https://pickup.example/show/credential/six@icloud.com",
            },
            {
                "email": "mixed@icloud.com",
                "token": "tok_mixed",
                "pickup_url": "https://pickup.example/show/credential/mixed@icloud.com",
            },
        ])

    @patch("webui.app.db.import_icloud_emails")
    def test_import_accepts_explicit_icloud_url_source(self, import_icloud):
        import_icloud.return_value = {"inserted": 1, "updated": 0, "skipped": 0, "invalid": 0}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_url",
            "text": "new-url@icloud.com----https://pickup.example/show/credential/new-url@icloud.com",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["parsed"], 1)
        import_icloud.assert_called_once_with([{
            "email": "new-url@icloud.com",
            "token": "",
            "pickup_url": "https://pickup.example/show/credential/new-url@icloud.com",
        }])

    @patch("webui.app.db.import_icloud_emails")
    def test_explicit_icloud_url_source_marks_token_only_line_invalid(self, import_icloud):
        import_icloud.return_value = {"inserted": 0, "updated": 0, "skipped": 0, "invalid": 1}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_url",
            "text": "token-only@icloud.com----tok_only",
        })

        self.assertEqual(response.status_code, 200)
        import_icloud.assert_called_once_with([{
            "email": "token-only@icloud.com",
            "token": "",
            "pickup_url": "",
        }])

    @patch("webui.app.db.list_icloud_email_pool")
    def test_list_icloud_pool_returns_only_masked_token(self, list_pool):
        list_pool.return_value = [{"id": 1, "email": "one@icloud.com", "status": "available", "token_masked": "tok_****1234"}]
        response = self.client.get("/api/outlook?source=icloud_api")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload[0]["source"], "icloud_api")
        self.assertNotIn("token", payload[0])

    @patch("webui.app.db.list_icloud_email_pool")
    def test_list_explicit_icloud_url_pool_uses_url_filter(self, list_pool):
        list_pool.return_value = [{
            "id": 2,
            "email": "url@icloud.com",
            "status": "available",
            "pickup_mode": "independent_url",
        }]
        response = self.client.get("/api/outlook?source=icloud_url")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload[0]["source"], "icloud_url")
        list_pool.assert_called_once_with(status=None, limit=500, pickup_filter="url")

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    @patch("webui.app.db.icloud_email_pool_summary", return_value={"available": 1, "total": 1})
    def test_jobs_warn_when_icloud_pool_is_smaller_than_count(self, summary, submit):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(email_config, "EMAIL_SOURCE", "icloud_api"):
            response = self.client.post("/api/jobs", json={"count": 2, "workers": 2})
        self.assertEqual(response.status_code, 200)
        self.assertIn("iCloud 邮箱池仅 1 个可用", response.get_json()["warning"])

    @patch("webui.app.db.delete_icloud_email", return_value=False)
    def test_delete_used_icloud_mailbox_reports_not_deleted(self, delete):
        response = self.client.post("/api/outlook/delete", json={"source": "icloud_api", "email": "one@icloud.com"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["deleted"])


if __name__ == "__main__":
    unittest.main()
