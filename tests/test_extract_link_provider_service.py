import unittest
from unittest.mock import patch

from core import extract_link_service
from core.kakao_extract_link_provider import (
    KakaoAcceptedBatch,
    KakaoBatchResult,
    KakaoExtractLinkError,
    build_kakao_batches,
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
    @staticmethod
    def _runtime_settings_with_proxy_switch(enabled):
        return lambda name, default=None: (
            enabled if name == "KAKAO_EXTRACT_USE_PROXY_POOL" else default
        )

    def test_provider_and_batch_size_validation(self):
        self.assertEqual(extract_link_service.provider_name("kakao"), "kakao_batch")
        self.assertEqual(extract_link_service.provider_name("legacy"), "legacy")
        self.assertEqual(extract_link_service.kakao_batch_size("5"), 5)

        with self.assertRaisesRegex(ValueError, "provider"):
            extract_link_service.provider_name("unknown")
        with self.assertRaisesRegex(ValueError, "1-5"):
            extract_link_service.kakao_batch_size(6)

    def test_make_kakao_client_uses_proxy_pool_by_default(self):
        proxy = "http://user:pass@kr.proxy:9000"
        with patch.object(
            extract_link_service,
            "_runtime_setting",
            side_effect=self._runtime_settings_with_proxy_switch(True),
        ), patch(
            "config.proxy.pick_proxy",
            return_value=proxy,
        ) as pick, patch(
            "config.proxy.PROXY_CHAIN_ENABLED",
            False,
        ), patch.object(
            extract_link_service,
            "KakaoExtractLinkClient",
        ) as client_type:
            extract_link_service._make_kakao_client(cdk="CDK")

        pick.assert_called_once_with()
        self.assertEqual(client_type.call_args.kwargs["proxy"], proxy)

    def test_make_kakao_client_skips_proxy_pool_when_switch_is_off(self):
        with patch.object(
            extract_link_service,
            "_runtime_setting",
            side_effect=self._runtime_settings_with_proxy_switch(False),
        ), patch("config.proxy.pick_proxy") as pick, patch.object(
            extract_link_service,
            "KakaoExtractLinkClient",
        ) as client_type:
            extract_link_service._make_kakao_client(cdk="CDK")

        pick.assert_not_called()
        self.assertEqual(client_type.call_args.kwargs["proxy"], "")

    def test_make_kakao_client_uses_direct_route_when_pool_is_empty(self):
        with patch.object(
            extract_link_service,
            "_runtime_setting",
            side_effect=self._runtime_settings_with_proxy_switch(True),
        ), patch(
            "config.proxy.pick_proxy",
            return_value="",
        ) as pick, patch.object(
            extract_link_service,
            "KakaoExtractLinkClient",
        ) as client_type:
            extract_link_service._make_kakao_client(cdk="CDK")

        pick.assert_called_once_with()
        self.assertEqual(client_type.call_args.kwargs["proxy"], "")

    def test_make_kakao_client_falls_back_to_direct_when_pick_fails(self):
        with patch.object(
            extract_link_service,
            "_runtime_setting",
            side_effect=self._runtime_settings_with_proxy_switch(True),
        ), patch(
            "config.proxy.pick_proxy",
            side_effect=RuntimeError("pool unavailable"),
        ) as pick, patch.object(
            extract_link_service,
            "KakaoExtractLinkClient",
        ) as client_type:
            extract_link_service._make_kakao_client(cdk="CDK")

        pick.assert_called_once_with()
        self.assertEqual(client_type.call_args.kwargs["proxy"], "")

    def test_kakao_batch_proxy_uses_local_bridge_when_chain_is_enabled(self):
        upstream = "http://user:pass@kr.proxy:9000"
        local = "http://session:bridge@127.0.0.1:25001"
        with patch.object(
            extract_link_service,
            "_bool_setting",
            return_value=True,
        ), patch(
            "config.proxy.pick_proxy",
            return_value=upstream,
        ), patch(
            "config.proxy.PROXY_CHAIN_ENABLED",
            True,
        ), patch(
            "core.roxybrowser_client.prepare_proxy_for_roxy",
            return_value=local,
        ) as prepare:
            selected = extract_link_service._kakao_batch_proxy()

        self.assertEqual(selected, local)
        prepare.assert_called_once_with(upstream)

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

    def test_kakao_batch_keeps_partial_success_when_server_cleans_batch(self):
        class PartialThenGoneClient(FakeKakaoClient):
            def poll(self, batch_id, *, on_update=None):
                partial = KakaoBatchResult(
                    batch_id=batch_id,
                    status="running",
                    done=False,
                    results=[
                        {"success": True, "paymentLink": "https://pay.example/recovered"},
                    ],
                    success_count=1,
                    failure_count=0,
                    charged_count=1,
                    remaining_count=3,
                )
                if on_update is not None:
                    on_update(partial)
                raise KakaoExtractLinkError(
                    "批次不存在或已被服务端清理",
                    http_status=404,
                )

        plan = build_kakao_batches([
            {"account_id": 100, "access_token": "TOKEN_A"},
            {"account_id": 101, "access_token": "TOKEN_B"},
        ], batch_size=5)[0]
        state = {
            100: {"id": 100, "extract_link_status": "queued"},
            101: {"id": 101, "extract_link_status": "queued"},
        }

        def update(account_id, payload):
            row = state[int(account_id)]
            row["extract_link_status"] = payload.get("status") or (
                "success" if payload.get("ok") else "failed"
            )
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if result.get("long_url"):
                row["extract_link_long_url"] = result["long_url"]
            row["last_payload"] = dict(payload)
            return True

        with patch.object(extract_link_service, "_QUEUE_SLOTS", FakeSlots()), \
                patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True), \
                patch.object(extract_link_service.db, "update_account_extract", side_effect=update), \
                patch.object(extract_link_service.db, "get_account", side_effect=lambda account_id: dict(state[int(account_id)])):
            result = extract_link_service._run_kakao_batch(
                plan=plan,
                client=PartialThenGoneClient(),
                trigger="manual_bulk",
                existing_batch_id="batch-cleaned",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(state[100]["extract_link_status"], "success")
        self.assertEqual(
            state[100]["extract_link_long_url"],
            "https://pay.example/recovered",
        )
        self.assertEqual(state[101]["extract_link_status"], "failed")


if __name__ == "__main__":
    unittest.main()
