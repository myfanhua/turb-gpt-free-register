import unittest
from unittest.mock import patch

from webui.app import _compact_account_for_list, create_app


def eligible_account(account_id=1):
    return {
        "id": account_id,
        "email": f"user{account_id}@example.com",
        "access_token": f"TOKEN_{account_id}",
        "plan_type": "free",
        "current_plan_type": "free",
        "plus_trial_eligible": True,
    }


class WebUiExtractLinkProviderTests(unittest.TestCase):
    def setUp(self):
        with patch("webui.app.db.recover_interrupted_extract_links", return_value={
            "failed_count": 0,
            "kakao_batches": [],
        }), patch(
            "webui.app.extract_link_service.resume_interrupted_kakao_batches",
            return_value={"resumed_batches": 0, "failed_batches": 0},
            create=True,
        ):
            self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.extract_link_service.latest_kakao_remaining_count", return_value=9, create=True)
    @patch("webui.app.extract_link_service.kakao_batch_size", return_value=5)
    @patch("webui.app.extract_link_service.provider_name", return_value="legacy")
    def test_options_returns_providers_and_saved_defaults(self, provider, batch_size, remaining):
        response = self.client.get("/api/extract-link/options")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["default_provider"], "legacy")
        self.assertEqual(payload["default_batch_size"], 5)
        self.assertEqual(payload["batch_size_min"], 1)
        self.assertEqual(payload["batch_size_max"], 5)
        self.assertEqual(payload["kakao_remaining_count"], 9)
        self.assertIn(
            {"value": "kakao_batch", "label": "Kakao API"},
            payload["providers"],
        )

    @patch("webui.app.config_editor.update_config")
    def test_save_defaults_only_writes_provider_and_batch_size(self, update_config):
        update_config.return_value = {
            "updated": ["EXTRACT_LINK_PROVIDER", "KAKAO_EXTRACT_BATCH_SIZE"],
            "ignored": [],
        }

        response = self.client.post("/api/extract-link/defaults", json={
            "provider": "kakao_batch",
            "batch_size": 3,
            "cdk": "must-not-be-written",
        })

        self.assertEqual(response.status_code, 200)
        update_config.assert_called_once_with({
            "EXTRACT_LINK_PROVIDER": "kakao_batch",
            "KAKAO_EXTRACT_BATCH_SIZE": 3,
        })

    @patch("webui.app.extract_link_service.enqueue_accounts_extract")
    @patch("webui.app.db.get_account")
    def test_single_route_forwards_current_provider(self, get_account, enqueue):
        get_account.return_value = eligible_account(1)
        enqueue.return_value = {
            "provider": "kakao_batch",
            "batch_count": 1,
            "started": [{"id": 1}],
            "started_count": 1,
            "busy": [],
            "busy_count": 0,
            "failed": [],
            "failed_count": 0,
        }

        response = self.client.post("/api/accounts/extract-link", json={
            "account_id": 1,
            "provider": "kakao_batch",
            "batch_size": 4,
        })

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["provider"], "kakao_batch")
        self.assertEqual(kwargs["batch_size"], 4)
        self.assertEqual([item["id"] for item in kwargs["accounts"]], [1])

    @patch("webui.app.extract_link_service.enqueue_accounts_extract")
    @patch("webui.app.db.get_account")
    def test_bulk_route_uses_only_selected_eligible_accounts(self, get_account, enqueue):
        accounts = {
            1: eligible_account(1),
            2: {**eligible_account(2), "plus_trial_eligible": False},
            3: eligible_account(3),
        }
        get_account.side_effect = lambda account_id: accounts.get(account_id)
        enqueue.return_value = {
            "provider": "kakao_batch",
            "batch_size": 2,
            "batch_count": 1,
            "started": [{"id": 1}, {"id": 3}],
            "started_count": 2,
            "busy": [],
            "busy_count": 0,
            "failed": [],
            "failed_count": 0,
        }

        response = self.client.post("/api/accounts/extract-link-bulk", json={
            "account_ids": [1, 2, 3],
            "provider": "kakao_batch",
            "batch_size": 2,
        })

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["batch_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual([item["id"] for item in kwargs["accounts"]], [1, 3])
        self.assertEqual(kwargs["batch_size"], 2)

    def test_compact_account_exposes_batch_status_without_secrets(self):
        row = {
            **eligible_account(1),
            "extract_link_status": "running",
            "extract_link_provider": "kakao_batch",
            "extract_link_batch_id": "batch-1",
            "extract_link_batch_number": 1,
            "extract_link_batch_total": 3,
            "extract_link_result_index": 0,
            "extract_link_cdk_remaining": 9,
            "extract_link_cdk": "SECRET-CDK",
        }

        compact = _compact_account_for_list(row)

        self.assertEqual(compact["extract_link_provider"], "kakao_batch")
        self.assertEqual(compact["extract_link_batch_number"], 1)
        self.assertEqual(compact["extract_link_batch_total"], 3)
        self.assertNotIn("access_token", compact)
        self.assertNotIn("extract_link_cdk", compact)


if __name__ == "__main__":
    unittest.main()
