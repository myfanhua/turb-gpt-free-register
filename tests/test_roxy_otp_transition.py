# -*- coding: utf-8 -*-
import inspect
import unittest
from unittest.mock import Mock, patch

from core import roxy_registration as registration


class RoxyOtpTransitionTests(unittest.TestCase):
    def test_wait_after_otp_submit_uses_longer_transition_window(self):
        timeout = inspect.signature(
            registration._wait_after_email_otp_submit
        ).parameters["timeout"].default

        self.assertEqual(timeout, 30)

    @patch("core.roxy_registration.time.sleep")
    @patch("core.roxy_registration.time.time", side_effect=[0.0, 0.1, 2.0, 2.1])
    @patch("core.roxy_registration._email_otp_page_state", return_value={"inputs": [], "errors": []})
    @patch("core.roxy_registration._is_email_verification_page", return_value=True)
    @patch("core.roxy_registration._is_profile_page", return_value=True)
    def test_wait_after_otp_submit_prioritizes_profile_page(
        self,
        profile_page,
        verification_page,
        page_state,
        clock,
        sleep,
    ):
        driver = Mock()

        self.assertEqual(
            registration._wait_after_email_otp_submit(driver, timeout=1),
            "accepted",
        )
        profile_page.assert_called()

    @patch("core.roxy_registration.time.sleep")
    @patch("core.roxy_registration.time.time", side_effect=[0.0, 0.1, 0.2])
    @patch(
        "core.roxy_registration._email_otp_page_state",
        return_value={
            "inputs": [],
            "errors": ["错误代码: account_deactivated"],
            "text": "账号已被删除或停用 account_deactivated",
        },
    )
    @patch("core.roxy_registration._is_email_verification_page", return_value=True)
    @patch("core.roxy_registration._is_profile_page", return_value=False)
    def test_wait_after_otp_submit_reports_deactivated_account_as_terminal(
        self,
        profile_page,
        verification_page,
        page_state,
        clock,
        sleep,
    ):
        driver = Mock()

        self.assertEqual(
            registration._wait_after_email_otp_submit(driver, timeout=1),
            "account_deactivated",
        )

    @patch("core.roxy_registration.time.sleep")
    @patch("core.roxy_registration.time.time", side_effect=[0.0, 0.1, 2.0])
    @patch("core.roxy_registration._is_profile_page", return_value=True)
    def test_resend_stops_when_page_has_already_advanced(self, profile_page, clock, sleep):
        driver = Mock()

        result = registration._click_resend_email_otp(driver, timeout=1)

        self.assertEqual(result, {"ok": False, "advanced": True})
        driver.execute_script.assert_not_called()

    @patch("core.roxy_registration.time.sleep")
    @patch("core.roxy_registration.time.time", side_effect=[0.0, 0.1, 0.2])
    @patch("core.roxy_registration._is_profile_page", side_effect=[False, True])
    def test_resend_rechecks_page_after_stale_element(self, profile_page, clock, sleep):
        class StaleButton:
            @property
            def text(self):
                raise RuntimeError("stale element reference")

        driver = Mock()
        driver.execute_script.side_effect = [StaleButton(), None]

        result = registration._click_resend_email_otp(driver, timeout=1)

        self.assertEqual(result, {"ok": False, "advanced": True})
        self.assertEqual(profile_page.call_count, 2)

    def test_session_wait_runs_abort_checker(self):
        driver = Mock()
        abort = Mock(side_effect=RuntimeError("stopped"))

        with self.assertRaisesRegex(RuntimeError, "stopped"):
            registration._fetch_chatgpt_session(
                driver,
                timeout=1,
                abort_checker=abort,
            )

        abort.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
