import unittest
from unittest.mock import patch

from core import extract_link_service
from core.kakao_extract_link_provider import (
    KakaoAcceptedBatch,
    KakaoBatchResult,
)


class ImmediateExecutor:
    def submit(self, fn, **kwargs):
        result = fn(**kwargs)

        class FinishedFuture:
            def result(self):
                return result

        return FinishedFuture()


class FakeSlots:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.acquired = 0
        self.released = 0

    def acquire(self, blocking=False):
        if self.allowed:
            self.acquired += 1
            return True
        return False

    def release(self):
        self.released += 1


class FakeKakaoClient:
    def __init__(self, *, results=None):
        self.results = results or [
            {"success": True, "paymentLink": "https://pay.example/a"},
        ]
        self.submitted = []
        self.polled = []

    def submit(self, tokens):
        self.submitted.append(list(tokens))
        return KakaoAcceptedBatch(batch_id="batch-1", request_id="request-1")

    def poll(self, batch_id, **kwargs):
        self.polled.append(batch_id)
        return KakaoBatchResult(
            batch_id=batch_id,
            status="completed",
            done=True,
            results=list(self.results),
            success_count=sum(1 for item in self.results if item.get("success")),
            failure_count=sum(1 for item in self.results if not item.get("success")),
            charged_count=sum(1 for item in self.results if item.get("success")),
            remaining_count=9,
        )


class ExtractLinkProviderServiceTests(unittest.TestCase):
    def test_provider_and_batch_size_validation(self):
        self.assertEqual(extract_link_service.provider_name("kakao"), "kakao_batch")
        self.assertEqual(extract_link_service.provider_name("legacy"), "legacy")
        self.assertEqual(extract_link_service.kakao_batch_size("5"), 5)

        with self.assertRaisesRegex(ValueError, "provider"):
            extract_link_service.provider_name("unknown")
        with self.assertRaisesRegex(ValueError, "1-5"):
            extract_link_service.kakao_batch_size(6)

    @patch.object(extract_link_service, "enqueue_account_extract")
    def test_legacy_bulk_keeps_per_account_queue_path(self, enqueue):
        enqueue.side_effect = [
            {"accepted": True, "busy": False, "link_type": "pix"},
            {"accepted": False, "busy": True, "error": "正在提链"},
        ]

        result = extract_link_service.enqueue_accounts_extract(
            accounts=[
                {"id": 1, "email": "a@example.com", "access_token": "TOKEN_A"},
                {"id": 2, "email": "b@example.com", "access_token": "TOKEN_B"},
            ],
            provider="legacy",
        )

        self.assertEqual(result["provider"], "legacy")
        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["busy_count"], 1)
        self.assertEqual(enqueue.call_count, 2)

    def test_kakao_bulk_submits_one_batch_and_updates_each_account(self):
        client = FakeKakaoClient(results=[
            {"success": True, "paymentLink": "https://pay.example/a"},
            {"success": False, "error": "资格不符"},
        ])
        slots = FakeSlots()
        updates = []

        with patch.object(extract_link_service, "_EXECUTOR", ImmediateExecutor()), \
                patch.object(extract_link_service, "_QUEUE_SLOTS", slots), \
                patch.object(extract_link_service, "_make_kakao_client", return_value=client), \
                patch.object(extract_link_service.db, "claim_account_extract", return_value=True), \
                patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True), \
                patch.object(
                    extract_link_service.db,
                    "update_account_extract",
                    side_effect=lambda account_id, result: updates.append((account_id, result)) or True,
                ):
            result = extract_link_service.enqueue_accounts_extract(
                accounts=[
                    {"id": 1, "email": "a@example.com", "access_token": "TOKEN_A"},
                    {"id": 2, "email": "b@example.com", "access_token": "TOKEN_B"},
                ],
                trigger="manual_bulk",
                provider="kakao_batch",
                batch_size=5,
            )

        self.assertEqual(result["provider"], "kakao_batch")
        self.assertEqual(result["batch_count"], 1)
        self.assertEqual(result["started_count"], 2)
        self.assertEqual(client.submitted, [["TOKEN_A", "TOKEN_B"]])
        final_by_id = {
            account_id: payload
            for account_id, payload in updates
            if payload.get("status") in {"success", "failed"}
        }
        self.assertEqual(final_by_id[1]["status"], "success")
        self.assertEqual(final_by_id[2]["error"], "资格不符")
        self.assertEqual(slots.released, 1)

    def test_kakao_bulk_reports_busy_account_without_submitting_it(self):
        client = FakeKakaoClient()

        def claim(account_id, *args, **kwargs):
            return int(account_id) == 1

        with patch.object(extract_link_service, "_EXECUTOR", ImmediateExecutor()), \
                patch.object(extract_link_service, "_QUEUE_SLOTS", FakeSlots()), \
                patch.object(extract_link_service, "_make_kakao_client", return_value=client), \
                patch.object(extract_link_service.db, "claim_account_extract", side_effect=claim), \
                patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True), \
                patch.object(extract_link_service.db, "update_account_extract", return_value=True):
            result = extract_link_service.enqueue_accounts_extract(
                accounts=[
                    {"id": 1, "email": "a@example.com", "access_token": "TOKEN_A"},
                    {"id": 2, "email": "b@example.com", "access_token": "TOKEN_B"},
                ],
                provider="kakao_batch",
                batch_size=5,
            )

        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["busy_count"], 1)
        self.assertEqual(client.submitted, [["TOKEN_A"]])

    def test_resume_existing_batch_only_polls_and_never_submits(self):
        client = FakeKakaoClient()
        slots = FakeSlots()

        with patch.object(extract_link_service, "_EXECUTOR", ImmediateExecutor()), \
                patch.object(extract_link_service, "_QUEUE_SLOTS", slots), \
                patch.object(extract_link_service, "_make_kakao_client", return_value=client), \
                patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True), \
                patch.object(extract_link_service.db, "update_account_extract", return_value=True):
            result = extract_link_service.resume_interrupted_kakao_batches([{
                "batch_id": "batch-existing",
                "batch_number": 1,
                "batch_total": 1,
                "accounts": [
                    {"account_id": 10, "result_index": 0},
                ],
            }])

        self.assertEqual(result["resumed_batches"], 1)
        self.assertEqual(client.submitted, [])
        self.assertEqual(client.polled, ["batch-existing"])
        self.assertEqual(slots.released, 1)


if __name__ == "__main__":
    unittest.main()
