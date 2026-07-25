# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import registration_service
from core import db


class RegistrationSourceAttributionTests(unittest.TestCase):
    @patch("core.registration_service.db.update_job")
    @patch("core.email_provider.resolve_email_source", return_value="assurivo")
    def test_actual_claimed_source_replaces_submit_snapshot(self, resolve, update_job):
        source = registration_service._record_actual_email_source(9, "person@example.com")

        self.assertEqual(source, "assurivo")
        update_job.assert_called_once_with(9, email="person@example.com", email_source="assurivo")
        resolve.assert_called_once_with("person@example.com")

    def test_db_can_update_only_email_source_without_changing_other_job_fields(self):
        with patch("core.db._load_jobs", return_value=[{"id": 9, "email": "person@example.com", "email_source": "generic_api", "status": "stopped"}]), \
             patch("core.db._save_jobs") as save:
            db.update_job(9, email_source="assurivo")

        saved = save.call_args.args[0][0]
        self.assertEqual(saved["email_source"], "assurivo")
        self.assertEqual(saved["email"], "person@example.com")
        self.assertEqual(saved["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
