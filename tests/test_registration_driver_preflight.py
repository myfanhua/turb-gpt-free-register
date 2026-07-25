# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from config import browser_use as browser_use_config
from config import roxybrowser as roxy_config
from core import registration_service
from webui.app import create_app


class RegistrationDriverPreflightTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_protocol_is_the_default_and_needs_no_browser_use_key(self):
        with patch.object(roxy_config, "REGISTRATION_DRIVER", "protocol"), \
             patch.object(browser_use_config, "BROWSER_USE_API_KEY", ""):
            result = registration_service.registration_driver_preflight()

        self.assertEqual(result["driver"], "protocol")
        self.assertTrue(result["can_submit"])
        self.assertEqual(result["required"], [])

    def test_browser_use_without_key_is_not_ready(self):
        with patch.object(roxy_config, "REGISTRATION_DRIVER", "browser_use"), \
             patch.object(browser_use_config, "BROWSER_USE_API_KEY", ""):
            result = registration_service.registration_driver_preflight()

        self.assertFalse(result["can_submit"])
        self.assertEqual(result["missing"], ["BROWSER_USE_API_KEY"])
        self.assertIn("不会创建注册任务", result["message"])

    @patch("webui.app.svc.submit_registration")
    def test_webui_rejects_browser_use_without_creating_jobs(self, submit_registration):
        with patch.object(roxy_config, "REGISTRATION_DRIVER", "browser_use"), \
             patch.object(browser_use_config, "BROWSER_USE_API_KEY", ""):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("BROWSER_USE_API_KEY", response.get_json()["error"])
        submit_registration.assert_not_called()

    def test_preflight_endpoint_exposes_current_driver_without_secret(self):
        with patch.object(roxy_config, "REGISTRATION_DRIVER", "browser_use"), \
             patch.object(browser_use_config, "BROWSER_USE_API_KEY", ""):
            response = self.client.get("/api/registration-preflight")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["required"], ["BROWSER_USE_API_KEY"])
        self.assertNotIn("api_key", payload)

    def test_service_guard_rejects_before_writing_a_job(self):
        with patch.object(roxy_config, "REGISTRATION_DRIVER", "browser_use"), \
             patch.object(browser_use_config, "BROWSER_USE_API_KEY", ""), \
             patch("core.registration_service.db.create_job") as create_job:
            with self.assertRaisesRegex(ValueError, "不会创建注册任务"):
                registration_service.submit_registration(count=1)

        create_job.assert_not_called()

    def test_register_page_contains_driver_status_panel(self):
        page = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="regDriverStatus"', page)
        self.assertIn("/api/registration-preflight", page)


if __name__ == "__main__":
    unittest.main()
