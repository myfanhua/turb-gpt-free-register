import unittest

from core.icloud_mail_client import ICloudProviderUnavailableError
from core.roxy_registration import _should_retry_otp_fetch


class RoxyOtpRetryPolicyTests(unittest.TestCase):
    def test_provider_outage_stops_resend_loop(self):
        error = ICloudProviderUnavailableError("iCloud 接码服务连续异常")

        self.assertFalse(_should_retry_otp_fetch(error))

    def test_regular_mail_timeout_keeps_existing_resend_behavior(self):
        self.assertTrue(_should_retry_otp_fetch(RuntimeError("等待验证码超时")))


if __name__ == "__main__":
    unittest.main()
