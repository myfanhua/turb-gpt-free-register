# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import roxy_registration as registration


class _Element:
    def __init__(self):
        self.clicks = 0

    def click(self):
        self.clicks += 1


class _Driver:
    current_url = "https://chatgpt.com/auth/login"

    def __init__(self, result=None):
        self.result = result or {}

    def execute_script(self, *_args):
        return self.result


class _Clock:
    def __init__(self):
        self.value = -1.0

    def __call__(self):
        self.value += 1.0
        return self.value


class RoxyEmailSubmitTests(unittest.TestCase):
    @patch("core.roxy_registration._is_oauth_consent_like", return_value=False)
    @patch("core.roxy_registration.time.sleep")
    def test_safe_submit_uses_native_element_click(self, _sleep, _oauth):
        button = _Element()
        driver = _Driver({
            "ok": True,
            "reason": "selected_primary_submit",
            "primary": True,
            "targetAttrs": "submit btn-primary",
            "target": button,
        })

        self.assertTrue(registration._submit_nearest_form_for_active_input(driver))
        self.assertEqual(button.clicks, 1)

    @patch("core.roxy_registration._is_email_login_page_still_present", return_value=True)
    @patch("core.roxy_registration._email_input_value_state")
    @patch("core.roxy_registration._is_signup_password_page", return_value=False)
    @patch("core.roxy_registration._is_email_verification_page", return_value=False)
    @patch("core.roxy_registration._is_login_password_page", return_value=False)
    @patch("core.roxy_registration._has_access_token", return_value=False)
    @patch("core.roxy_registration.time.sleep")
    def test_cleared_email_waits_for_full_transition_timeout_before_retry(
        self,
        _sleep,
        _access,
        _login_password,
        _otp,
        _signup_password,
        email_state,
        _still_present,
    ):
        email_state.return_value = {
            "url": "https://chatgpt.com/auth/login?email=one%40icloud.com",
            "inputs": [{"value": ""}],
        }
        clock = _Clock()
        with patch("core.roxy_registration.time.time", side_effect=clock):
            state = registration._wait_email_submit_next_state(
                _Driver(), "one@icloud.com", timeout=10
            )

        self.assertEqual(state, "email_page")

    @patch("core.roxy_registration.time.sleep")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._navigate_auth_via_nextauth", return_value=True)
    @patch("core.roxy_registration._wait_email_submit_next_state", side_effect=["email_page", "otp"])
    @patch("core.roxy_registration._submit_email_step")
    @patch("core.roxy_registration._email_input_value_state")
    @patch("core.roxy_registration._type_email_address")
    def test_stuck_chatgpt_email_page_uses_nextauth_fallback_without_refilling(
        self,
        type_email,
        email_state,
        _submit,
        _wait,
        fallback,
        _human_delay,
        _sleep,
    ):
        email_state.return_value = {
            "url": "https://chatgpt.com/auth/login",
            "inputs": [{"value": "one@icloud.com"}],
        }
        driver = _Driver()

        state = registration._submit_email_and_wait_next(driver, "one@icloud.com", attempts=3)

        self.assertEqual(state, "otp")
        self.assertEqual(type_email.call_count, 1)
        fallback.assert_called_once_with(driver, "one@icloud.com")

    @patch("core.roxy_registration.time.sleep")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._wait_email_submit_next_state", return_value="login_password")
    @patch("core.roxy_registration._submit_email_step")
    @patch("core.roxy_registration._email_input_value_state")
    @patch("core.roxy_registration._type_email_address")
    def test_recovery_mode_returns_login_password_state(
        self,
        _type_email,
        email_state,
        _submit,
        _wait,
        _human_delay,
        _sleep,
    ):
        email_state.return_value = {
            "url": "https://chatgpt.com/auth/login",
            "inputs": [{"value": "saved@example.com"}],
        }

        state = registration._submit_email_and_wait_next(
            _Driver(),
            "saved@example.com",
            allow_login_password=True,
        )

        self.assertEqual(state, "login_password")


if __name__ == "__main__":
    unittest.main()
