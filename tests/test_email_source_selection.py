# -*- coding: utf-8 -*-
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from config import email as email_config
from config import register as register_config
from core import email_provider
from core import registration_service

try:
    from webui.app import create_app
except ModuleNotFoundError as exc:
    create_app = None
    WEBUI_IMPORT_ERROR = str(exc)
else:
    WEBUI_IMPORT_ERROR = ""


class EmailSourceOverrideTests(unittest.TestCase):
    def test_explicit_source_overrides_global_order(self):
        with patch.object(email_config, "EMAIL_SOURCE", "outlook,generic_api"), patch.object(
            email_provider, "_pick_from_source", side_effect=lambda source, excluded: source
        ) as pick:
            with email_provider.email_source_context("generic_api"):
                self.assertEqual(email_provider.acquire_email(), "generic_api")

        pick.assert_called_once_with("generic_api", set())

    def test_concurrent_tasks_keep_separate_source_contexts(self):
        barrier = threading.Barrier(2)

        def run(source):
            with email_provider.email_source_context(source):
                barrier.wait(timeout=5)
                return email_provider.acquire_email()

        with patch.object(email_provider, "_pick_from_source", side_effect=lambda source, excluded: source):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run, ("outlook", "generic_api")))

        self.assertEqual(results, ["outlook", "generic_api"])

    def test_submission_mode_distinguishes_default_from_explicit_single_source(self):
        executor = Mock()
        created = []

        def create_job(*, email_source, email_source_mode):
            job = {
                "id": len(created) + 1,
                "log_file": "unused.log",
                "email_source": email_source,
                "email_source_mode": email_source_mode,
            }
            created.append(job)
            return job

        with patch.object(email_config, "EMAIL_SOURCE", "generic_api"), patch.object(
            registration_service, "get_executor", return_value=executor
        ), patch.object(registration_service, "get_executor_workers", return_value=1), patch.object(
            registration_service.db, "create_job", side_effect=create_job
        ), patch.object(registration_service.db, "get_job", side_effect=lambda job_id: created[job_id - 1]):
            registration_service.submit_registration(count=1, workers=1)
            registration_service.submit_registration(
                count=1, email_source="generic_api", workers=1
            )

        self.assertEqual(
            [job["email_source_mode"] for job in created], ["auto", "specific"]
        )

    def test_otp_wait_uses_task_source_before_address_detection(self):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            email_provider, "_registered_email_source", return_value=None
        ), patch.object(email_provider, "resolve_email_source", return_value="outlook"), patch(
            "core.generic_api_mail_client.fetch_latest_otp", return_value="123456"
        ) as fetch_otp:
            with email_provider.email_source_context("generic_api"):
                code = email_provider.wait_for_otp("generic@example.com", after_ts=10)

        self.assertEqual(code, "123456")
        fetch_otp.assert_called_once_with("generic@example.com", after_ts=10)

    def test_browser_acquisition_uses_override_when_service_flag_is_off(self):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            email_provider, "acquire_email", return_value="generic@example.com"
        ) as acquire:
            with email_provider.email_source_context("generic_api"):
                result = email_provider.acquire_email_after_input()

        self.assertEqual(result, "generic@example.com")
        acquire.assert_called_once_with()


@unittest.skipIf(create_app is None, f"WebUI dependency unavailable: {WEBUI_IMPORT_ERROR}")
class RegistrationEmailSourceApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.generic_api_email_pool_summary", return_value={"available": 2})
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_explicit_generic_api_source_uses_pool_even_in_manual_config(
        self, submit_registration, generic_pool_summary
    ):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            email_config, "EMAIL_SOURCE", "outlook"
        ), patch.object(register_config, "REGISTER_EMAIL", "fixed@example.com"):
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "workers": 1, "email_source": "generic_api"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        generic_pool_summary.assert_called_once_with()
        submit_registration.assert_called_once_with(
            count=1, email_source="generic_api", workers=1
        )

    def test_source_options_always_include_generic_api(self):
        response = self.client.get("/api/registration/email-sources")

        self.assertEqual(response.status_code, 200)
        options = response.get_json()["options"]
        self.assertIn("generic_api", [option["value"] for option in options])
