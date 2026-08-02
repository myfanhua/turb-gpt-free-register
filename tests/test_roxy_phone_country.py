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


if __name__ == "__main__":
    unittest.main()
