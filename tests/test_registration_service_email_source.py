# -*- coding: utf-8 -*-
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, call, patch

from core import registration_service as svc


class RegistrationServiceEmailSourceTests(unittest.TestCase):
    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="one@icloud.com")
    def test_prepare_registration_args_uses_explicit_job_source(self, acquire, birthday):
        with patch("config.register.REGISTER_EMAIL", ""), \
             patch("config.register.REGISTER_NAME", "Test User"), \
             patch("config.email.USE_EMAIL_SERVICE", True):
            email, name, value = svc._prepare_registration_args("icloud_url")

        self.assertEqual((email, name, value), ("one@icloud.com", "Test User", "1990-01-01"))
        acquire.assert_called_once_with("icloud_url")

    @patch("main.run_registration", return_value={"success": True, "email": "one@icloud.com", "account_id": 9})
    @patch("core.registration_service._prepare_registration_args", return_value=("one@icloud.com", "Test User", "1990-01-01"))
    @patch("core.registration_service.db.update_job")
    @patch("core.registration_service.db.get_job")
    def test_worker_reads_source_from_its_job_snapshot(self, get_job, update_job, prepare, run):
        with tempfile.TemporaryDirectory() as tempdir:
            job = {
                "id": 7,
                "status": "pending",
                "email_source": "icloud_url",
                "log_file": str(Path(tempdir) / "job.log"),
            }
            get_job.return_value = job

            svc._run_one_job(7, job["log_file"])

        prepare.assert_called_once_with("icloud_url")

    @patch("core.registration_service.db.get_job")
    @patch("core.registration_service.db.create_job")
    @patch("core.registration_service.get_executor_workers", return_value=2)
    @patch("core.registration_service.get_executor")
    def test_submit_registration_snapshots_current_config(self, get_executor, workers, create_job, get_job):
        executor = Mock()
        get_executor.return_value = executor
        create_job.side_effect = lambda email_source: {
            "id": 1,
            "email_source": email_source,
            "log_file": "job.log",
        }
        get_job.side_effect = lambda job_id: {
            "id": job_id,
            "email_source": "icloud_api,outlook",
            "log_file": "job.log",
        }

        with patch("config.email.EMAIL_SOURCE", "icloud_api,outlook"):
            jobs = svc.submit_registration(count=1, email_source=None, workers=2)

        create_job.assert_called_once_with(email_source="icloud_api,outlook")
        self.assertEqual(jobs[0]["email_source"], "icloud_api,outlook")

    @patch("core.registration_service.db.get_job")
    @patch("core.registration_service.db.create_job")
    @patch("core.registration_service.get_executor_workers", return_value=2)
    @patch("core.registration_service.get_executor")
    def test_submit_registration_preserves_explicit_virtual_source(self, get_executor, workers, create_job, get_job):
        executor = Mock()
        get_executor.return_value = executor
        create_job.side_effect = lambda email_source: {
            "id": 1,
            "email_source": email_source,
            "log_file": "job.log",
        }
        get_job.side_effect = lambda job_id: {
            "id": job_id,
            "email_source": "icloud_api_token",
            "log_file": "job.log",
        }

        svc.submit_registration(count=1, email_source="icloud_api_token", workers=2)

        create_job.assert_called_once_with(email_source="icloud_api_token")

    @patch("main.run_registration")
    @patch("core.registration_service._prepare_registration_args")
    @patch("core.registration_service.db.update_job")
    @patch("core.registration_service.db.get_job")
    def test_two_concurrent_workers_keep_different_job_sources(self, get_job, update_job, prepare, run):
        with tempfile.TemporaryDirectory() as tempdir:
            jobs = {
                1: {"id": 1, "status": "pending", "email_source": "icloud_url", "log_file": str(Path(tempdir) / "one.log")},
                2: {"id": 2, "status": "pending", "email_source": "outlook", "log_file": str(Path(tempdir) / "two.log")},
            }
            get_job.side_effect = lambda job_id: jobs[int(job_id)]

            def prepare_args(source):
                return (f"{source}@example.com", "Test User", "1990-01-01")

            prepare.side_effect = prepare_args
            run.side_effect = lambda email, name, birthday: {
                "success": True,
                "email": email,
                "account_id": 1,
            }

            for _ in range(20):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(svc._run_one_job, job_id, row["log_file"])
                        for job_id, row in jobs.items()
                    ]
                    for future in futures:
                        future.result()

        self.assertCountEqual(
            prepare.call_args_list,
            [call("icloud_url"), call("outlook")] * 20,
        )


if __name__ == "__main__":
    unittest.main()
