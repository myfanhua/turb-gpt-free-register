# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class TrialPaymentRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def test_single_probe_route_enqueues_without_returning_token(self):
        account = {"id": 7, "email": "fixture@example.test", "access_token": "secret-token"}
        with patch("webui.app.db.get_account", return_value=account), patch(
            "webui.app.trial_payment_service.enqueue_account_trial_payment_probe",
            return_value={"accepted": True, "busy": False, "account_id": 7, "email": account["email"], "status": "queued"},
        ) as enqueue:
            response = self.client.post(
                "/api/accounts/check-trial-payments",
                headers={"X-Auth-Code": "test-auth", "Content-Type": "application/json"},
                json={"account_id": 7, "country": "ID"},
            )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertNotIn("secret-token", repr(payload))
        self.assertEqual(enqueue.call_args.kwargs["country"], "ID")

    def test_bulk_probe_route_rejects_more_than_500(self):
        response = self.client.post(
            "/api/accounts/check-trial-payments-bulk",
            headers={"X-Auth-Code": "test-auth", "Content-Type": "application/json"},
            json={"account_ids": list(range(501))},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
