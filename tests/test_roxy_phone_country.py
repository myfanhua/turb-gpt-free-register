# -*- coding: utf-8 -*-
import unittest

from core import roxy_codex_oauth


class RoxyPhoneCountryTests(unittest.TestCase):
    def test_uk_number_provides_gb_select_hints(self):
        hints = roxy_codex_oauth._phone_country_selection_hints("+447529516135")

        self.assertEqual(hints["dial_code"], "44")
        self.assertEqual(hints["iso2"], "GB")
        self.assertIn("United Kingdom", hints["names"])
        self.assertIn("英国", hints["names"])

    def test_longest_prefix_wins_for_morocco(self):
        hints = roxy_codex_oauth._phone_country_selection_hints("+212612345678")

        self.assertEqual(hints["dial_code"], "212")
        self.assertEqual(hints["iso2"], "MA")

    def test_set_phone_value_passes_country_hints_to_page_script(self):
        class _Driver:
            def __init__(self):
                self.fill_args = None

            def execute_script(self, script, *args):
                if not args:
                    return True
                self.fill_args = args
                return {
                    "ok": True,
                    "e164": "+447529516135",
                    "visibleValue": "7529516135",
                    "actualVisible": "7529516135",
                    "hiddenValue": "+447529516135",
                }

        driver = _Driver()
        roxy_codex_oauth._set_phone_value(driver, "+447529516135")

        self.assertEqual(driver.fill_args[1]["iso2"], "GB")
        self.assertEqual(driver.fill_args[1]["dial_code"], "44")

    def test_verify_accepts_national_visible_value_with_full_hidden_e164(self):
        class _Driver:
            def execute_script(self, script, *args):
                return {
                    "ok": False,
                    "visibleValue": "9758618929",
                    "hiddenValue": "+639758618929",
                    "expected": "+639758618929",
                    "visibleDigits": "9758618929",
                    "hiddenDigits": "639758618929",
                    "expectedDigits": "639758618929",
                    "url": "https://auth.openai.com/add-phone",
                }

        result = roxy_codex_oauth._verify_add_phone_value_before_submit(
            _Driver(), "+639758618929"
        )

        self.assertEqual(result["visibleValue"], "9758618929")

    def test_verify_rejects_unrelated_visible_value(self):
        self.assertFalse(
            roxy_codex_oauth._phone_values_match(
                "+639758618929", "5551234", "+639758618929"
            )
        )


if __name__ == "__main__":
    unittest.main()
