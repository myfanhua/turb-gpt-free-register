import unittest

from core.kakao_extract_link_provider import (
    KakaoExtractLinkClient,
    KakaoExtractLinkError,
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


if __name__ == "__main__":
    unittest.main()
