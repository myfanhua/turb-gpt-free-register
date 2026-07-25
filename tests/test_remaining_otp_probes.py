# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import cloudmail_client, mailnest_client, outlook_client, qqmail_client


class RemainingOtpProbeTests(unittest.TestCase):
    @patch("core.mailnest_client.time.monotonic", side_effect=[0, 0, 5])
    @patch("core.mailnest_client._get_mails")
    def test_mailnest_pending_candidate_completed_and_after_filter(self, get_mails, clock):
        get_mails.return_value = [
            {"code_match": "111111", "timestamp": 100},
            {"code_match": "654321", "timestamp": 205},
        ]
        state = mailnest_client.MailNestProbeState()
        first = mailnest_client.fetch_otp_once("x@test", 200, state, settle_seconds=5)
        second = mailnest_client.fetch_otp_once("x@test", 200, state, settle_seconds=5)
        self.assertEqual((first.status, second.status, second.code), ("candidate", "completed", "654321"))

    @patch("core.cloudmail_client.time.monotonic", side_effect=[0, 0, 5])
    @patch("core.cloudmail_client._request")
    def test_cloudmail_pending_candidate_completed_and_after_filter(self, request, clock):
        request.return_value = [
            {"sendEmail": "noreply@openai.com", "subject": "Code 111111", "content": "code 111111", "createTime": "1970-01-01 00:01:40"},
            {"sendEmail": "noreply@openai.com", "subject": "Code 654321", "content": "code 654321", "createTime": "1970-01-01 00:03:25"},
        ]
        state = cloudmail_client.CloudMailProbeState()
        first = cloudmail_client.fetch_otp_once("x@test", 200, state, settle_seconds=5)
        second = cloudmail_client.fetch_otp_once("x@test", 200, state, settle_seconds=5)
        self.assertEqual((first.status, second.status, second.code), ("candidate", "completed", "654321"))

    @patch("core.qqmail_client.time.monotonic", side_effect=[0, 0, 5])
    @patch("core.qqmail_client._search_messages")
    @patch("core.qqmail_client._connect_imap")
    def test_qq_probe_candidate_completed_and_after_filter(self, connect, search, clock):
        mail = Mock(); connect.return_value = mail
        search.return_value = [
            {"from": "noreply@openai.com", "to": "x@test", "subject": "Code 111111", "text": "code 111111", "date": "1970-01-01T00:01:40+00:00"},
            {"from": "noreply@openai.com", "to": "x@test", "subject": "Code 654321", "text": "code 654321", "date": "1970-01-01T00:03:25+00:00"},
        ]
        state = qqmail_client.QQMailProbeState()
        first = qqmail_client.fetch_otp_once("x@test", 200, state, settle_seconds=5)
        second = qqmail_client.fetch_otp_once("x@test", 200, state, settle_seconds=5)
        self.assertEqual((first.status, second.status, second.code), ("candidate", "completed", "654321"))
        self.assertTrue(mail.logout.called)

    @patch("core.outlook_client.time.monotonic", side_effect=[0, 0, 5])
    @patch("core.outlook_client._fetch_via")
    @patch("core.outlook_client._http_session")
    def test_outlook_probe_candidate_completed_and_after_filter(self, session_factory, fetch_via, clock):
        account = outlook_client.OutlookAccount("x@test", "p", "id", "refresh")
        outlook_client._CONTEXT_CACHE[account.email] = account
        session_factory.return_value = Mock()
        fresh = {"from": "noreply@openai.com", "subject": "Code 654321", "text": "code 654321", "receivedDateTime": "1970-01-01T00:03:25Z"}
        old = {"from": "noreply@openai.com", "subject": "Code 111111", "text": "code 111111", "receivedDateTime": "1970-01-01T00:01:40Z"}
        fetch_via.side_effect = lambda _s, protocol, _a: [old, fresh] if protocol == "graph" else []
        state = outlook_client.OutlookProbeState()
        first = outlook_client.fetch_otp_once(account.email, 200, state, settle_seconds=5)
        second = outlook_client.fetch_otp_once(account.email, 200, state, settle_seconds=5)
        self.assertEqual((first.status, second.status, second.code), ("candidate", "completed", "654321"))

    def test_new_wrappers_keep_legacy_signature_and_disable_unavailable_retrigger(self):
        account = outlook_client.OutlookAccount("outlook@test", "p", "id", "refresh")
        outlook_client._CONTEXT_CACHE[account.email] = account
        cases = [
            (outlook_client, "outlook@test"),
            (qqmail_client, "qq@test"),
            (mailnest_client, "mailnest@test"),
            (cloudmail_client, "cloudmail@test"),
        ]
        for module, email in cases:
            with self.subTest(module=module.__name__), patch.object(module, "wait_for_otp_with_policy", return_value="654321") as policy:
                self.assertEqual(module.fetch_latest_otp(email, after_ts=0, max_wait=1, poll_interval=1, settle_seconds=0), "654321")
                self.assertEqual(policy.call_args.kwargs["retry_count"], 0)


if __name__ == "__main__":
    unittest.main()
