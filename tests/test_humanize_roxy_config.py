# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch


class HumanizeRoxyConfigTests(unittest.TestCase):
    def test_runtime_defaults_expose_balanced_task_pacing(self):
        from config import humanize as cfg

        required = (
            "HUMANIZE_PACING_PROFILE",
            "HUMANIZE_REVIEW_BEFORE_SUBMIT",
            "HUMANIZE_MOUSE_TRAJECTORY",
        )
        missing = [name for name in required if not hasattr(cfg, name)]
        self.assertEqual(missing, [], f"missing humanize config: {missing}")
        self.assertEqual(cfg.HUMANIZE_PACING_PROFILE, "balanced")
        self.assertTrue(cfg.HUMANIZE_REVIEW_BEFORE_SUBMIT)
        self.assertTrue(cfg.HUMANIZE_MOUSE_TRAJECTORY)

        from config import roxybrowser as roxy_cfg
        self.assertTrue(roxy_cfg.ROXY_FINGERPRINT_FOLLOW_PROXY_IP)
        self.assertEqual(roxy_cfg.ROXY_MAX_CONCURRENT_PROFILES, 2)

    def test_webui_exposes_pacing_and_native_roxy_controls(self):
        from webui.config_editor import EDITABLE_FIELDS

        keys = {field["key"] for field in EDITABLE_FIELDS}
        self.assertTrue({
            "HUMANIZE_PACING_PROFILE",
            "HUMANIZE_REVIEW_BEFORE_SUBMIT",
            "HUMANIZE_MOUSE_TRAJECTORY",
            "ROXY_NATIVE_FINGERPRINT_ONLY",
            "ROXY_FINGERPRINT_FOLLOW_PROXY_IP",
            "ROXY_MAX_CONCURRENT_PROFILES",
        }.issubset(keys))

    def test_env_template_exposes_the_complete_pacing_and_native_profile_switches(self):
        text = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text(encoding="utf-8")
        for key in (
            "ROXY_RANDOM_OS_ON_CREATE",
            "ROXY_RANDOM_OS_CHOICES",
            "ROXY_RANDOM_FINGERPRINT_ON_CREATE",
            "ROXY_NATIVE_FINGERPRINT_ONLY",
            "ROXY_FINGERPRINT_FOLLOW_PROXY_IP",
            "ROXY_MAX_CONCURRENT_PROFILES",
            "ROXY_RANDOM_PROFILE_NAME_ON_CREATE",
            "ENABLE_HUMANIZE_BROWSER_ACTIONS",
            "HUMANIZE_PACING_PROFILE",
            "HUMANIZE_REVIEW_BEFORE_SUBMIT",
            "HUMANIZE_MOUSE_TRAJECTORY",
        ):
            self.assertIn(f"{key}=", text)

    def test_native_fingerprint_mode_skips_manual_javascript_overrides(self):
        from config import roxybrowser as roxy_cfg
        from core.roxy_registration import _apply_browser_automation_mask

        class Driver:
            def __init__(self):
                self.calls = []

            def execute_cdp_cmd(self, name, payload):
                self.calls.append(("cdp", name, payload))

            def execute_script(self, script):
                self.calls.append(("script", script))

        driver = Driver()
        with patch.object(roxy_cfg, "ROXY_NATIVE_FINGERPRINT_ONLY", True, create=True):
            _apply_browser_automation_mask(driver)

        self.assertEqual(driver.calls, [])

    def test_manual_mask_remains_available_when_native_only_is_disabled(self):
        from config import roxybrowser as roxy_cfg
        from core.roxy_registration import _apply_browser_automation_mask

        class Driver:
            def __init__(self):
                self.calls = []

            def execute_cdp_cmd(self, name, payload):
                self.calls.append(("cdp", name))

            def execute_script(self, script):
                self.calls.append(("script",))

        driver = Driver()
        with patch.object(roxy_cfg, "ROXY_NATIVE_FINGERPRINT_ONLY", False, create=True):
            _apply_browser_automation_mask(driver)

        self.assertTrue(driver.calls)

    def test_registration_entry_owns_one_task_pacing_scope_and_explicit_stages(self):
        source = Path(__file__).resolve().parents[1].joinpath("core", "roxy_registration.py").read_text(encoding="utf-8")

        self.assertIn("@paced_task(interrupt_check=_check_manual_stop)", source)
        for stage in ("navigate", "email", "password", "otp", "profile", "post_auth", "codex"):
            self.assertIn(f'set_pacing_stage("{stage}")', source)

    def test_otp_is_always_typed_in_precise_mode_and_reviewed_before_submit(self):
        source = Path(__file__).resolve().parents[1].joinpath("core", "roxy_registration.py").read_text(encoding="utf-8")

        self.assertIn("_human_type_text(driver, els[0], code, clear=True, precise=True)", source)
        self.assertIn('context.review("otp_submit")', source)

    def test_submit_and_resend_pauses_use_the_task_pacing_engine(self):
        source = Path(__file__).resolve().parents[1].joinpath("core", "roxy_registration.py").read_text(encoding="utf-8")

        self.assertNotIn("time.sleep(random.uniform", source)
        self.assertIn('context.review("email_submit")', source)
        self.assertIn('context.review("profile_submit")', source)

    def test_roxy_profile_variation_uses_task_local_rng(self):
        source = Path(__file__).resolve().parents[1].joinpath("core", "roxybrowser_client.py").read_text(encoding="utf-8")

        self.assertIn("from core.humanize import pacing_rng", source)
        self.assertIn("rng = pacing_rng()", source)
        self.assertNotIn("random.choice(", source)
        self.assertNotIn("random.randrange(", source)


if __name__ == "__main__":
    unittest.main()
