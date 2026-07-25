# -*- coding: utf-8 -*-
import unittest

from core.otp_wait_policy import OTPProbeResult, OTPWaitExhausted, resolve_wait_timeout, wait_for_otp_with_policy


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class OtpWaitPolicyTests(unittest.TestCase):
    def test_first_fetch_hit_returns_without_wait_or_retrigger(self):
        clock = FakeClock()
        calls = []
        result = wait_for_otp_with_policy(
            provider="mail", email="user@example.com", initial_after_ts=10,
            fetch=lambda after_ts: calls.append(after_ts) or "123456",
            retrigger=lambda: self.fail("不应重发"), clock=clock.monotonic, wait=clock.wait,
            wait_timeout=20, poll_interval=5, retry_count=3,
        )
        self.assertEqual(result, "123456")
        self.assertEqual(calls, [10])
        self.assertEqual(clock.waits, [])

    def test_empty_polls_then_hit_in_same_round(self):
        clock = FakeClock()
        answers = iter([None, None, "654321"])
        result = wait_for_otp_with_policy(
            provider="mail", email="user@example.com", initial_after_ts=10,
            fetch=lambda _: next(answers), retrigger=lambda: self.fail("不应重发"),
            clock=clock.monotonic, wait=clock.wait, wait_timeout=20, poll_interval=5, retry_count=0,
        )
        self.assertEqual(result, "654321")
        self.assertEqual(clock.waits, [5, 5])

    def test_timeout_retrigger_then_second_round_hit_updates_after_ts(self):
        clock = FakeClock()
        fetched_after = []
        retriggers = []
        def fetch(after_ts):
            fetched_after.append(after_ts)
            return "112233" if after_ts == 101 else None
        def retrigger():
            retriggers.append(True)
            return 101
        result = wait_for_otp_with_policy(
            provider="mail", email="user@example.com", initial_after_ts=100,
            fetch=fetch, retrigger=retrigger, clock=clock.monotonic, wait=clock.wait,
            wait_timeout=10, poll_interval=5, retry_count=1,
        )
        self.assertEqual(result, "112233")
        self.assertEqual(retriggers, [True])
        self.assertEqual(fetched_after, [100, 100, 101])

    def test_exhaustion_contains_redacted_context_and_last_error(self):
        clock = FakeClock()
        def fetch(_): raise RuntimeError("inbox unavailable")
        with self.assertRaises(OTPWaitExhausted) as raised:
            wait_for_otp_with_policy(
                provider="mail", email="private@example.com", initial_after_ts=1,
                fetch=fetch, retrigger=lambda: 2, clock=clock.monotonic, wait=clock.wait,
                wait_timeout=5, poll_interval=5, retry_count=1,
            )
        exc = raised.exception
        self.assertEqual(exc.rounds, 2)
        self.assertEqual(exc.provider, "mail")
        self.assertNotIn("private@example.com", str(exc))
        self.assertIn("inbox unavailable", str(exc))

    def test_retry_zero_is_exactly_one_round(self):
        clock = FakeClock()
        retriggered = []
        with self.assertRaises(OTPWaitExhausted) as raised:
            wait_for_otp_with_policy(
                provider="mail", email="a@b.com", initial_after_ts=1,
                fetch=lambda _: None, retrigger=lambda: retriggered.append(True) or 2,
                clock=clock.monotonic, wait=clock.wait, wait_timeout=5, poll_interval=5, retry_count=0,
            )
        self.assertEqual(raised.exception.rounds, 1)
        self.assertEqual(retriggered, [])

    def test_timeout_priority_uses_new_setting_then_legacy_alias(self):
        self.assertEqual(resolve_wait_timeout(120, 90), 120)
        self.assertEqual(resolve_wait_timeout(None, 90), 90)

    def test_probe_candidate_is_compatible_and_waits_for_completed(self):
        clock = FakeClock(); answers = iter([OTPProbeResult.candidate({"otp":"1"}), OTPProbeResult.completed("123456")])
        self.assertEqual(wait_for_otp_with_policy(provider="x", email="a@b.com", initial_after_ts=1, fetch=lambda _: next(answers), retrigger=lambda: 2, clock=clock.monotonic, wait=clock.wait, wait_timeout=10, poll_interval=5, retry_count=0), "123456")
        self.assertEqual(clock.waits, [5])

    def test_candidate_ready_at_shortens_wait_without_busy_loop(self):
        clock = FakeClock(); answers = iter([OTPProbeResult.candidate(ready_at_monotonic=2), OTPProbeResult.completed("123456")])
        self.assertEqual(wait_for_otp_with_policy(provider="x", email="a@b.com", initial_after_ts=1, fetch=lambda _: next(answers), retrigger=lambda: 2, clock=clock.monotonic, wait=clock.wait, wait_timeout=10, poll_interval=5, retry_count=0), "123456")
        self.assertEqual(clock.waits, [2])


if __name__ == "__main__":
    unittest.main()
