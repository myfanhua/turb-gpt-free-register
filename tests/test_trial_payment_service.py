# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db, trial_payment_service


class TrialPaymentServiceTests(unittest.TestCase):
    def test_claim_and_update_trial_payment_probe_state(self):
        rows = [{"id": 7, "email": "fixture@example.test", "access_token": "token"}]
        saved = []

        def save(value):
            saved[:] = value

        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts", side_effect=save):
            self.assertTrue(db.claim_account_trial_payment_probe(7))
            self.assertEqual(rows[0]["trial_payment_probe_status"], "queued")
            self.assertTrue(db.mark_account_trial_payment_probe_running(7))
            self.assertEqual(rows[0]["trial_payment_probe_status"], "running")
            self.assertTrue(db.update_account_trial_payment_probe(7, {
                "ok": True,
                "checked_at": "2026-09-02T12:00:00",
                "country": "ID",
                "campaign_id": "plus-1-month-free",
                "methods": [{"id": "momo", "label": "Momo", "status": "supported"}],
                "available_method_types": ["card", "momo"],
            }))

        self.assertEqual(rows[0]["trial_payment_probe_status"], "success")
        self.assertEqual(rows[0]["trial_payment_methods"][0]["status"], "supported")
        self.assertEqual(rows[0]["trial_payment_available_types"], ["card", "momo"])
        self.assertTrue(saved)

    def test_worker_resolves_registration_country_and_writes_result(self):
        account = {"id": 8, "email": "fixture@example.test", "access_token": "token"}
        result = {"ok": True, "checked_at": "2026-09-02T12:00:00", "country": "JP", "methods": []}
        with patch.object(trial_payment_service.db, "mark_account_trial_payment_probe_running", return_value=True), \
                patch.object(trial_payment_service, "resolve_account_plan_proxy", return_value=("proxy://masked", {"proxy_provider": "cliproxy_traffic", "proxy_country": "JP"})), \
                patch.object(trial_payment_service, "probe_trial_payment_methods", return_value=result) as probe, \
                patch.object(trial_payment_service.db, "update_account_trial_payment_probe", return_value=True):
            value = trial_payment_service.run_account_trial_payment_probe(
                account_id=8,
                email=account["email"],
                access_token=account["access_token"],
                proxy=None,
            )

        self.assertTrue(value["ok"])
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs["country"], "JP")
        self.assertEqual(probe.call_args.kwargs["proxy"], "proxy://masked")


if __name__ == "__main__":
    unittest.main()
