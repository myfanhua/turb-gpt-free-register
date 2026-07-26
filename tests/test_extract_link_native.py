# -*- coding: utf-8 -*-
import time
import unittest
from unittest.mock import call, patch

from core import extract_link_service as service


class ExtractLinkNativeTests(unittest.TestCase):
    def test_native_create_queues_access_token_and_email(self):
        with patch.object(service, "_json_request", return_value={"job_id": "job-1"}) as request:
            result = service._create_native_job(
                token="ACCESS_TOKEN",
                email="user@example.com",
                link_type="upi",
            )

        self.assertEqual(result["job_id"], "job-1")
        request.assert_called_once_with(
            "POST",
            "/api/upi/session-jobs",
            payload={
                "accessToken": "ACCESS_TOKEN",
                "email": "user@example.com",
                "notify_telegram": False,
            },
        )

    def test_native_poll_maps_link_and_qr_result(self):
        responses = [
            {"status": "running", "error": None},
            {
                "status": "success",
                "return_url": "https://payments.example/link",
                "has_qr": True,
                "qr_expires_at": 1_900_000_000,
            },
        ]
        with (
            patch.object(service, "_json_request", side_effect=responses),
            patch.object(service, "_api_base", return_value="http://127.0.0.1:8085"),
            patch.object(service.time, "sleep"),
        ):
            events = list(service._iter_native_events(job_id="job id"))

        result = next(data["result"] for event, data in events if event == "result")
        self.assertEqual(result["long_url"], "https://payments.example/link")
        self.assertEqual(result["copy_paste"], "https://payments.example/link")
        self.assertEqual(result["image_url_png"], "http://127.0.0.1:8085/api/upi/jobs/job%20id/qr")
        self.assertEqual(result["expires_at"], 1_900_000_000)

    def test_duplicate_expired_job_is_retried(self):
        duplicate = service._JsonHttpError(409, {"detail": "duplicate"})
        existing = {
            "jobs": [{
                "id": "old-job",
                "email": "user@example.com",
                "status": "success",
                "has_qr": True,
                "qr_expires_at": time.time() - 10,
                "created_at": 10,
            }],
        }
        with patch.object(
            service,
            "_json_request",
            side_effect=[duplicate, existing, {"ok": True}],
        ) as request:
            result = service._create_native_job(
                token="ACCESS_TOKEN",
                email="user@example.com",
                link_type="upi",
            )

        self.assertEqual(result["job_id"], "old-job")
        self.assertFalse(result["reused"])
        self.assertEqual(
            request.call_args_list[-1],
            call("POST", "/api/upi/jobs/old-job/retry", payload={}),
        )

    def test_native_mode_does_not_require_cdk(self):
        with (
            patch.object(service, "_api_mode", return_value="upi_native"),
            patch.object(service, "_create_native_job", return_value={"job_id": "job-2"}) as create,
        ):
            result = service._create_extract_job(
                token="ACCESS_TOKEN",
                email="user@example.com",
                link_type="upi",
                cdk="",
            )

        self.assertEqual(result["job_id"], "job-2")
        create.assert_called_once_with(
            token="ACCESS_TOKEN",
            email="user@example.com",
            link_type="upi",
        )


if __name__ == "__main__":
    unittest.main()
