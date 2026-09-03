# -*- coding: utf-8 -*-
import unittest

import core.humanize as humanize


class _FakeClock:
    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))


class PacingEngineContractTests(unittest.TestCase):
    def setUp(self):
        required = (
            "PacingPolicy",
            "PacingDecision",
            "PacingContext",
            "pacing_scope",
            "current_pacing",
            "set_stage",
        )
        missing = [name for name in required if not hasattr(humanize, name)]
        self.assertEqual(missing, [], f"missing task-level pacing API: {missing}")

    def _policy(self, **overrides):
        values = {
            "enabled": True,
            "factor": 1.0,
            "profile": "balanced",
            "review_before_submit": True,
            "mouse_trajectory": True,
            "ranges": {
                "click": (0.10, 0.40),
                "review": (0.20, 0.50),
                "keystroke": (0.01, 0.04),
            },
        }
        values.update(overrides)
        return humanize.PacingPolicy(**values)

    def _sequence(self, seed):
        clock = _FakeClock()
        with humanize.pacing_scope(seed=seed, policy=self._policy(), clock=clock) as ctx:
            humanize.set_stage("email")
            values = [humanize.delay("click") for _ in range(5)]
            ledger = list(ctx.ledger)
        return values, clock.sleeps, ledger

    def test_same_seed_replays_the_same_decisions_without_global_rng(self):
        first = self._sequence(20260829)
        second = self._sequence(20260829)
        other = self._sequence(20260830)

        self.assertEqual(first, second)
        self.assertNotEqual(first[0], other[0])
        self.assertTrue(all(0.10 <= value <= 0.40 for value in first[0]))

    def test_delay_uses_injected_clock_and_records_only_redacted_metadata(self):
        values, sleeps, ledger = self._sequence(7)

        self.assertEqual(values, sleeps)
        self.assertEqual(len(ledger), 5)
        self.assertTrue(all(item["kind"] == "click" for item in ledger))
        self.assertTrue(all(item["stage"] == "email" for item in ledger))
        forbidden = {"text", "value", "email", "password", "otp", "proxy"}
        self.assertTrue(all(not (forbidden & set(item)) for item in ledger))

    def test_typing_is_bursty_but_precise_fields_stay_character_by_character(self):
        with humanize.pacing_scope(seed=11, policy=self._policy()) as ctx:
            chunks = ctx.typing_chunks("hello@example.test", precise=False)
            precise = ctx.typing_chunks("123456", precise=True)

        self.assertEqual("".join(chunks), "hello@example.test")
        self.assertTrue(all(1 <= len(chunk) <= 3 for chunk in chunks))
        self.assertEqual(precise, list("123456"))

    def test_mouse_path_has_multiple_steps_and_finishes_on_target(self):
        with humanize.pacing_scope(seed=23, policy=self._policy()) as ctx:
            points = ctx.mouse_path((20.0, 30.0), (420.0, 260.0))

        self.assertGreaterEqual(len(points), 4)
        self.assertEqual(points[-1], (420.0, 260.0))
        self.assertTrue(all(isinstance(x, float) and isinstance(y, float) for x, y in points))

    def test_review_budget_allows_only_one_pause_per_submit_label_and_stage(self):
        clock = _FakeClock()
        policy = self._policy(review_probability=1.0)
        with humanize.pacing_scope(seed=31, policy=policy, clock=clock) as ctx:
            humanize.set_stage("profile")
            first = ctx.review("profile_submit")
            second = ctx.review("profile_submit")

        self.assertGreater(first, 0.0)
        self.assertEqual(second, 0.0)
        self.assertEqual(len(clock.sleeps), 1)

    def test_scope_resets_active_context(self):
        self.assertIsNone(humanize.current_pacing(create=False))
        with humanize.pacing_scope(seed=1, policy=self._policy()) as ctx:
            self.assertIs(humanize.current_pacing(create=False), ctx)
        self.assertIsNone(humanize.current_pacing(create=False))


if __name__ == "__main__":
    unittest.main()
