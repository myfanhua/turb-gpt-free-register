# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import assurivo_mail_client as client


class AssurivoMailClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.material = self.root / "assurivo.txt"
        self.state = self.root / "assurivo.json"
        self.paths = patch.multiple(client, _ACCOUNTS_FILE=self.material, _STATE_FILE=self.state)
        self.paths.start()
        self.config_path = patch.object(client._email_cfg, "ASSURIVO_ACCOUNTS_FILE", self.material.name)
        self.config_path.start()
        client._CONTEXT_CACHE.clear()

    def tearDown(self):
        self.paths.stop()
        self.config_path.stop()
        self.tmp.cleanup()

    def test_parse_material_requires_matching_complete_assurivo_url(self):
        url = "https://assurivo.com/console/open.php?mail=user%40example.com&pwd=secret-query-code&limit=20"
        item = client.parse_material_line("user@example.com----" + url)
        self.assertEqual(item.email, "user@example.com")
        self.assertEqual(item.query_url, url)
        self.assertIsNone(client.parse_material_line("user@example.com"))
        self.assertIsNone(client.parse_material_line("not-an-email----https://assurivo.com/console/open.php?mail=x"))
        self.assertIsNone(client.parse_material_line("user@example.com----https://assurivo.com/console/open.php?mail=other%40example.com&pwd=x"))

    def test_pick_release_and_unconsumed_recovery_use_independent_state(self):
        self.material.write_text("a@example.com----https://assurivo.com/console/open.php?mail=a%40example.com&pwd=query-a\n", encoding="utf-8")
        account = client.pick_account()
        self.assertEqual(account.email, "a@example.com")
        self.assertIn("pwd=query-a", client.get_account_context(account.email).query_url)
        self.assertTrue(client.release_unconsumed_account(account.email, "cancelled"))
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))[0]["status"], "available")
        client.release_account(account.email, status="failed", note="bad")
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))[0]["status"], "failed")

    def test_nested_messages_filter_old_unknown_and_non_openai(self):
        payload = {"data": {"emails": [
            {"from": "noreply@openai.com", "subject": "Verification code", "text": "111111", "timestamp": 90},
            {"from": "shop@example.com", "subject": "Your code", "text": "222222", "timestamp": 120},
            {"from": "noreply@openai.com", "subject": "Your verification code", "html": "<b>654321</b>", "timestamp": 120},
            {"from": "noreply@openai.com", "subject": "Verification code", "text": "333333"},
        ]}}
        self.assertEqual(client.extract_new_openai_otp(payload, after_ts=100), "654321")

    def test_snapshot_allows_small_clock_skew_but_rejects_old_or_window_outside_codes(self):
        def payload(stamp, code):
            return {"emails": [{
                "from": "noreply@openai.com", "subject": "Verification code",
                "text": code, "timestamp": stamp,
            }]}

        self.assertEqual(client.extract_new_openai_otp(payload(170, "111111"), after_ts=200, known_otp_fingerprints=set()), "111111")
        old_fingerprint = {client.hashlib.sha256(b"222222").hexdigest()}
        self.assertIsNone(client.extract_new_openai_otp(payload(170, "222222"), after_ts=200, known_otp_fingerprints=old_fingerprint))
        self.assertIsNone(client.extract_new_openai_otp(payload(10, "333333"), after_ts=200, known_otp_fingerprints=set()))
        self.assertIsNone(client.extract_new_openai_otp(payload(170, "444444"), after_ts=200, known_otp_fingerprints=None))

    def test_html_and_text_code_extractors_reject_long_numbers(self):
        message = {"from": "noreply@openai.com", "subject": "Your verification code", "html": "<p>Code: 246810</p>", "timestamp": 200}
        self.assertEqual(client.extract_message_otp(message), "246810")
        message["html"] = "Order 1234567; reference 12345678"
        self.assertIsNone(client.extract_message_otp(message))

    def test_html_response_requires_openai_verification_context(self):
        self.assertEqual(client.extract_assurivo_html_otp("<div>OpenAI verification code: <b>246810</b></div>"), "246810")
        self.assertIsNone(client.extract_assurivo_html_otp("<div>order code: 246810</div>"))

    def test_html_response_extracts_escaped_mail_code_away_from_verification_copy(self):
        payload = (
            "<style>.x{color:#ffffff}</style>OpenAI verification email "
            "&lt;!--[if mso]&gt;246810&lt;![endif]--&gt;"
        )
        self.assertEqual(client.extract_assurivo_html_otp(payload), "246810")

    def test_html_freshness_requires_full_received_at_after_request(self):
        page = (
            "OpenAI verification email 2026-07-25 04:02:15Z 111111 "
            "OpenAI verification email 2026-07-25 04:03:02Z 222222"
        )
        after = client.datetime.fromisoformat("2026-07-25T04:02:30+00:00").timestamp()
        self.assertEqual(client._fresh_html_otp_candidates(page, after), ["222222"])
        self.assertEqual(client._fresh_html_otp_candidates("OpenAI verification email 04:03:02 222222", after), [])

    def test_html_naive_time_uses_configured_shanghai_timezone(self):
        page = "OpenAI verification 2026-07-25 03:14:01 111111 OpenAI verification 2026-07-25 04:03:02 222222"
        after = client.datetime.fromisoformat("2026-07-25T04:00:00+08:00").timestamp()
        with patch.object(client._email_cfg, "ASSURIVO_TIMEZONE", "Asia/Shanghai"):
            self.assertEqual(client._fresh_html_otp_candidates(page, after), ["222222"])

    def test_empty_html_is_classified_without_treating_it_as_otp(self):
        html = "<div>OpenAI verification mailbox is empty</div>"
        self.assertTrue(client.is_empty_assurivo_html(html))
        self.assertIsNone(client.extract_assurivo_html_otp(html))

    @patch("core.assurivo_mail_client.fetch_otp_once")
    def test_probe_waits_for_quiet_window_and_resets_on_newer_code(self, fetch):
        now = [0.0]
        state = client.AssurivoOtpState()
        fetch.return_value = "111111"
        first = client.probe_otp_once("a@example.com", 1, state, settle_seconds=60, clock=lambda: now[0])
        self.assertEqual(first.status, "candidate")
        now[0] = 30.0; fetch.return_value = "222222"
        changed = client.probe_otp_once("a@example.com", 1, state, settle_seconds=60, clock=lambda: now[0])
        self.assertEqual(changed.status, "candidate")
        self.assertEqual(changed.ready_at_monotonic, 90.0)
        now[0] = 90.0
        completed = client.probe_otp_once("a@example.com", 1, state, settle_seconds=60, clock=lambda: now[0])
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.code, "222222")

    @patch("core.assurivo_mail_client.fetch_otp_once", return_value="654321")
    def test_probe_logs_candidate_remaining_time_and_completion(self, _fetch):
        state = client.AssurivoOtpState()
        with self.assertLogs(client.logger, level="INFO") as logs:
            first = client.probe_otp_once("a@example.com", 1, state, settle_seconds=60, clock=lambda: 0)
            waiting = client.probe_otp_once("a@example.com", 1, state, settle_seconds=60, clock=lambda: 7.2)
            completed = client.probe_otp_once("a@example.com", 1, state, settle_seconds=60, clock=lambda: 60)
        text = "\n".join(logs.output)
        self.assertEqual((first.status, waiting.status, completed.status), ("candidate", "candidate", "completed"))
        self.assertIn("候选更新=True，稳定窗口剩余=60s，状态=candidate", text)
        self.assertIn("候选更新=False，稳定窗口剩余=53s，状态=candidate", text)
        self.assertIn("稳定窗口剩余=0s，状态=completed", text)

    @patch("core.assurivo_mail_client.requests.get")
    def test_http_request_preserves_url_timeout_and_does_not_leak_query_code(self, get):
        url = "https://assurivo.com/console/open.php?mail=a%40example.com&pwd=very-secret-token&limit=20"
        self.material.write_text("a@example.com----" + url + "\n", encoding="utf-8")
        client.pick_account()
        response = Mock(status_code=200)
        response.json.return_value = {"emails": []}
        get.return_value = response
        with self.assertLogs(client.logger, level="INFO") as logs:
            self.assertIsNone(client.fetch_otp_once("a@example.com", after_ts=1))
        self.assertEqual(get.call_args.args[0], url)
        self.assertNotIn("params", get.call_args.kwargs)
        self.assertEqual(get.call_args.kwargs["timeout"], 20)
        self.assertNotIn("very-secret-token", "\n".join(logs.output))

    @patch("core.assurivo_mail_client.requests.get")
    def test_http_json_and_network_errors_are_redacted(self, get):
        self.material.write_text("a@example.com----https://assurivo.com/console/open.php?mail=a%40example.com&pwd=very-secret-token\n", encoding="utf-8")
        client.pick_account()
        response = Mock(status_code=500, text="server error")
        get.return_value = response
        with self.assertRaises(client.AssurivoMailError) as raised:
            client.fetch_otp_once("a@example.com", after_ts=1)
        self.assertNotIn("very-secret-token", str(raised.exception))
        get.side_effect = client.requests.RequestException("network down")
        with self.assertRaises(client.AssurivoMailError):
            client.fetch_otp_once("a@example.com", after_ts=1)

    @patch("core.assurivo_mail_client.wait_for_otp_with_policy", return_value="123456")
    def test_wait_delegates_to_gate_one_policy_without_inventing_resend(self, policy):
        self.material.write_text("a@example.com----https://assurivo.com/console/open.php?mail=a%40example.com&pwd=very-secret-token\n", encoding="utf-8")
        client.pick_account()
        self.assertEqual(client.fetch_latest_otp("a@example.com", after_ts=10), "123456")
        self.assertEqual(policy.call_args.kwargs["provider"], "assurivo")
        self.assertEqual(policy.call_args.kwargs["retry_count"], 0)


if __name__ == "__main__":
    unittest.main()
