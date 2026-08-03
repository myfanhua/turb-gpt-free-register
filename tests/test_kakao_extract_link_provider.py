import unittest
from unittest.mock import patch

from core.kakao_extract_link_provider import (
    build_kakao_batches,
    KakaoExtractLinkClient,
    KakaoExtractLinkError,
    map_kakao_results,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, payload, timeout):
        self.calls.append({
            "method": method,
            "url": url,
            "payload": payload,
            "timeout": timeout,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class KakaoExtractLinkClientTests(unittest.TestCase):
    def make_client(self, transport, **kwargs):
        return KakaoExtractLinkClient(
            api_base="https://tiqu.dxmcs.xin/",
            cdk="KAKAO-CDK",
            timeout_seconds=930,
            poll_interval=0,
            request_timeout=30,
            transport=transport,
            sleep=lambda _: None,
            **kwargs,
        )

    def test_submit_builds_async_batch_payload(self):
        transport = FakeTransport([(
            202,
            {
                "ok": True,
                "accepted": True,
                "requestId": "request-1",
                "batchId": "batch-1",
                "status": "queued",
            },
        )])
        client = self.make_client(transport)

        accepted = client.submit(["TOKEN_A", "TOKEN_B"])

        self.assertEqual(accepted.batch_id, "batch-1")
        self.assertEqual(accepted.request_id, "request-1")
        self.assertEqual(transport.calls, [{
            "method": "POST",
            "url": "https://tiqu.dxmcs.xin/api/v1/extractions/async",
            "payload": {
                "accessTokens": ["TOKEN_A", "TOKEN_B"],
                "cdk": "KAKAO-CDK",
                "timeoutSeconds": 930,
            },
            "timeout": 30.0,
        }])

    def test_submit_rejects_more_than_five_tokens(self):
        client = self.make_client(FakeTransport([]))

        with self.assertRaisesRegex(ValueError, "1-5"):
            client.submit([f"TOKEN_{index}" for index in range(6)])

    def test_submit_requires_batch_id(self):
        client = self.make_client(FakeTransport([(202, {"accepted": True})]))

        with self.assertRaisesRegex(KakaoExtractLinkError, "batchId"):
            client.submit(["TOKEN_A"])

    def test_poll_retries_transient_get_failure_then_returns_result(self):
        transport = FakeTransport([
            TimeoutError("temporary timeout"),
            (200, {"batchId": "batch-1", "status": "running", "done": False}),
            (200, {
                "batchId": "batch-1",
                "status": "completed",
                "done": True,
                "successCount": 1,
                "failureCount": 0,
                "chargedCount": 1,
                "remainingCount": 9,
                "results": [{"success": True, "paymentLink": "https://pay.example/1"}],
            }),
        ])
        client = self.make_client(transport, max_poll_failures=2)

        result = client.poll("batch-1")

        self.assertTrue(result.done)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.results[0]["paymentLink"], "https://pay.example/1")
        self.assertEqual(result.remaining_count, 9)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET", "GET"])

    def test_documented_cdk_error_is_translated(self):
        transport = FakeTransport([(
            402,
            {"error": "CDK_QUOTA_INSUFFICIENT", "detail": "quota"},
        )])
        client = self.make_client(transport)

        with self.assertRaisesRegex(KakaoExtractLinkError, "剩余次数不足"):
            client.submit(["TOKEN_A"])

    def test_batch_not_found_is_translated(self):
        client = self.make_client(FakeTransport([(
            404,
            {"detail": "batch not found"},
        )]))

        with self.assertRaisesRegex(KakaoExtractLinkError, "批次不存在"):
            client.get_batch("missing")

    def test_terminal_error_status_preserves_service_reason(self):
        client = self.make_client(FakeTransport([(
            200,
            {
                "batchId": "batch-error",
                "status": "error",
                "done": True,
                "error": "CDK_QUOTA_EXHAUSTED",
                "results": [],
            },
        )]))

        with self.assertRaisesRegex(KakaoExtractLinkError, "次数已用完"):
            client.poll("batch-error")

    def test_error_messages_redact_cdk_and_tokens(self):
        transport = FakeTransport([RuntimeError(
            "request failed for TOKEN_SECRET with KAKAO-CDK at http://user:pass@example.test"
        )])
        client = self.make_client(transport)

        with self.assertRaises(KakaoExtractLinkError) as ctx:
            client.submit(["TOKEN_SECRET"])

        message = str(ctx.exception)
        self.assertNotIn("TOKEN_SECRET", message)
        self.assertNotIn("KAKAO-CDK", message)
        self.assertNotIn("user:pass", message)

    def test_curl_transport_reuses_client_proxy_for_submit_and_poll(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = ""

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeCurl:
            @staticmethod
            def request(method, url, **kwargs):
                calls.append((method, url, kwargs.get("proxy")))
                payload = (
                    {"batchId": "batch-1", "status": "queued"}
                    if method == "POST"
                    else {
                        "batchId": "batch-1",
                        "status": "completed",
                        "done": True,
                        "results": [],
                    }
                )
                return FakeResponse(payload)

        with patch("core.kakao_extract_link_provider.curl_requests", FakeCurl):
            client = self.make_client(
                None,
                proxy="http://user:pass@kr.proxy:9000",
            )
            client.submit(["TOKEN_A"])
            client.get_batch("batch-1")

        self.assertEqual(
            [item[2] for item in calls],
            [
                "http://user:pass@kr.proxy:9000",
                "http://user:pass@kr.proxy:9000",
            ],
        )

    def test_urllib_transport_installs_proxy_for_http_and_https(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"batchId":"batch-1","status":"queued"}'

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse()

        client = self.make_client(
            None,
            proxy="http://user:pass@kr.proxy:9000",
        )
        with patch("core.kakao_extract_link_provider.curl_requests", None), \
                patch("core.kakao_extract_link_provider.ProxyHandler") as handler_type, \
                patch("core.kakao_extract_link_provider.build_opener", return_value=FakeOpener()):
            status, payload = client._default_transport(
                "POST",
                "https://tiqu.dxmcs.xin/api/v1/extractions/async",
                {"ok": True},
                30,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["batchId"], "batch-1")
        handler_type.assert_called_once_with({
            "http": "http://user:pass@kr.proxy:9000",
            "https": "http://user:pass@kr.proxy:9000",
        })


class KakaoBatchPlanningTests(unittest.TestCase):
    def test_build_batches_deduplicates_tokens_and_preserves_account_mapping(self):
        batches = build_kakao_batches([
            {"account_id": 1, "email": "a@example.com", "access_token": "TOKEN_A"},
            {"account_id": 2, "email": "b@example.com", "access_token": "TOKEN_A"},
            {"account_id": 3, "email": "c@example.com", "access_token": "TOKEN_C"},
        ], batch_size=2)

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].tokens, ["TOKEN_A", "TOKEN_C"])
        self.assertEqual(batches[0].account_ids_by_result_index[0], [1, 2])
        self.assertEqual(batches[0].account_ids_by_result_index[1], [3])

    def test_twelve_unique_tokens_split_into_five_five_two(self):
        batches = build_kakao_batches([
            {
                "account_id": index,
                "email": f"user{index}@example.com",
                "access_token": f"TOKEN_{index}",
            }
            for index in range(1, 13)
        ], batch_size=5)

        self.assertEqual([len(batch.tokens) for batch in batches], [5, 5, 2])
        self.assertEqual([batch.batch_number for batch in batches], [1, 2, 3])
        self.assertTrue(all(batch.batch_total == 3 for batch in batches))

    def test_batch_size_must_be_between_one_and_five(self):
        items = [{"account_id": 1, "access_token": "TOKEN_A"}]

        for invalid in (0, 6):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "1-5"):
                    build_kakao_batches(items, batch_size=invalid)

    def test_map_results_keeps_partial_success_per_account(self):
        plan = build_kakao_batches([
            {"account_id": 1, "access_token": "TOKEN_A"},
            {"account_id": 2, "access_token": "TOKEN_A"},
            {"account_id": 3, "access_token": "TOKEN_B"},
        ], batch_size=5)[0]

        mapped = map_kakao_results(plan, [
            {"success": True, "paymentLink": "https://pay.example/a"},
            {"success": False, "error": "资格不符"},
        ])

        self.assertEqual(mapped[1]["result"]["long_url"], "https://pay.example/a")
        self.assertEqual(mapped[2]["result"]["long_url"], "https://pay.example/a")
        self.assertEqual(mapped[3]["status"], "failed")
        self.assertEqual(mapped[3]["error"], "资格不符")

    def test_result_count_mismatch_does_not_shift_or_succeed_missing_accounts(self):
        plan = build_kakao_batches([
            {"account_id": 10, "access_token": "TOKEN_A"},
            {"account_id": 11, "access_token": "TOKEN_B"},
        ], batch_size=5)[0]

        mapped = map_kakao_results(plan, [
            {"success": True, "paymentLink": "https://pay.example/a"},
        ])

        self.assertEqual(mapped[10]["status"], "success")
        self.assertEqual(mapped[11]["status"], "failed")
        self.assertIn("结果数量", mapped[11]["error"])


if __name__ == "__main__":
    unittest.main()
