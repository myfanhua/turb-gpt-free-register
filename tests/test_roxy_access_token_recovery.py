# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import roxy_access_token_recovery as recovery
from core.roxybrowser_client import RoxyOpenResult


class RoxyAccessTokenRecoveryTests(unittest.TestCase):
    def test_recovery_allows_slow_login_page_to_finish_loading(self):
        opened = Mock()
        driver = Mock()
        with patch.object(recovery, "RoxyBrowserClient", return_value=Mock()), \
             patch.object(
                 recovery,
                 "_open_roxy_registration_browser",
                 return_value=(opened, driver),
             ) as open_browser:
            result = recovery._open_browser(
                proxy="http://stored-proxy",
                device_id="device-1",
                should_stop=lambda: False,
            )

        self.assertEqual(result[1:], (opened, driver))
        self.assertEqual(open_browser.call_args.kwargs["email_ready_timeout"], 60)

    def test_login_password_switches_to_email_otp_and_returns_session(self):
        driver = Mock()
        opened = RoxyOpenResult(
            profile_id="profile-1",
            raw={},
            created_by_run=True,
            registration_proxy="http://sid-1:bridge@127.0.0.1:25001",
            account_device_id="device-1",
        )
        with patch.object(recovery, "_open_browser", return_value=(Mock(), opened, driver)), \
             patch.object(recovery, "_submit_email_and_wait_next", return_value="login_password"), \
             patch.object(recovery, "_click_passwordless_signup_if_present", return_value={"ok": True}), \
             patch.object(recovery, "_wait_for_otp_page_or_session", return_value="otp"), \
             patch.object(recovery, "_wait_for_otp_stoppable", return_value="123456"), \
             patch.object(recovery, "_clear_otp_inputs"), \
             patch.object(recovery, "_type_otp"), \
             patch.object(recovery, "_click_continue"), \
             patch.object(recovery, "_wait_after_email_otp_submit", return_value="accepted"), \
             patch.object(recovery, "_fetch_chatgpt_session", return_value={
                 "accessToken": "TOKEN_NEW",
                 "user": {"id": "user-1"},
             }):
            result = recovery.run_roxy_access_token_recovery(
                email="saved@example.com",
                proxy="http://stored-proxy",
                device_id="device-1",
                should_stop=lambda: False,
            )

        self.assertEqual(result["session_info"]["accessToken"], "TOKEN_NEW")
        self.assertEqual(result["device_id"], "device-1")
        self.assertEqual(
            result["proxy_used"],
            "http://sid-1:bridge@127.0.0.1:25001",
        )

    def test_signup_password_page_is_rejected(self):
        with patch.object(recovery, "_open_browser", return_value=(Mock(), Mock(), Mock())), \
             patch.object(recovery, "_submit_email_and_wait_next", return_value="password"):
            with self.assertRaisesRegex(RuntimeError, "创建账号密码页"):
                recovery.run_roxy_access_token_recovery(
                    email="saved@example.com",
                    proxy=None,
                    device_id="device-1",
                    should_stop=lambda: False,
                )

    def test_phone_verification_aborts_before_session_wait(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/phone-verification"
        driver.execute_script.side_effect = RuntimeError("unavailable")
        with self.assertRaises(recovery.PhoneVerificationRequired):
            recovery._check_abort(driver, lambda: False)

    def test_stop_signal_raises_recovery_stopped(self):
        with self.assertRaises(recovery.AccessTokenRecoveryStopped):
            recovery._check_abort(Mock(), lambda: True)


if __name__ == "__main__":
    unittest.main()
