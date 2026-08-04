import unittest
from unittest.mock import patch

from core.icloud_mail_client import ICloudProviderUnavailableError
from core import roxy_registration


class RoxyOtpRetryPolicyTests(unittest.TestCase):
    def test_provider_outage_stops_resend_loop(self):
        error = ICloudProviderUnavailableError("iCloud 接码服务连续异常")

        self.assertFalse(roxy_registration._should_retry_otp_fetch(error))

    def test_regular_mail_timeout_keeps_existing_resend_behavior(self):
        self.assertTrue(roxy_registration._should_retry_otp_fetch(RuntimeError("等待验证码超时")))

    @patch("core.roxy_registration.time.time", return_value=200.0)
    def test_fetch_timeout_keeps_original_otp_cutoff(self, now):
        self.assertEqual(
            roxy_registration._next_otp_after_ts(
                100.0,
                previous_code_rejected=False,
            ),
            100.0,
        )
        now.assert_not_called()

    @patch("core.roxy_registration.time.time", return_value=200.0)
    def test_rejected_code_advances_otp_cutoff(self, now):
        self.assertEqual(
            roxy_registration._next_otp_after_ts(
                100.0,
                previous_code_rejected=True,
            ),
            200.0,
        )
        now.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
