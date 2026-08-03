# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class WebUiCodexRetryPlusGateTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1]
            / "webui"
            / "templates"
            / "index.html"
        ).read_text(encoding="utf-8")

    def test_retry_confirmations_explain_plus_gate_before_otp_and_sms(self):
        self.assertIn("先实时确认当前已开通 Plus", self.html)
        self.assertIn("确认 Plus 后才会消耗邮箱 OTP 和接码短信", self.html)


if __name__ == "__main__":
    unittest.main()
