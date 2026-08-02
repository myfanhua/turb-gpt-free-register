# -*- coding: utf-8 -*-
import unittest
from unittest.mock import call, patch

from core import roxy_codex_oauth


class RoxyPhoneCountryTests(unittest.TestCase):
    def test_select_sms_channel_does_not_reclick_when_already_selected(self):
        class _Driver:
            def __init__(self):
                self.scripts = []

            def execute_script(self, script, *args):
                self.scripts.append(script)
                return True

        driver = _Driver()
        state = {
            "radios": [
                {"value": "sms", "checked": True},
                {"value": "whatsapp", "checked": False},
            ]
        }

        with patch.object(roxy_codex_oauth, "_phone_page_state", return_value=state):
            roxy_codex_oauth._select_sms_channel_or_raise(driver)

        self.assertEqual(driver.scripts, [])

    def test_prepare_phone_submission_selects_sms_before_typing_number(self):
        driver = object()
        calls = []

        with (
            patch.object(
                roxy_codex_oauth,
                "_select_sms_channel_or_raise",
                side_effect=lambda _driver: calls.append("select_sms"),
            ),
            patch.object(
                roxy_codex_oauth,
                "_blur_active_input_and_wait",
                side_effect=lambda _driver, label="": calls.append(label),
            ),
            patch.object(
                roxy_codex_oauth,
                "_set_phone_value",
                side_effect=lambda _driver, phone, timeout=10: (
                    calls.append("type_phone")
                    or {
                        "e164": phone,
                        "actualVisible": "350 3568165",
                        "hiddenValue": phone,
                    }
                ),
            ),
            patch.object(
                roxy_codex_oauth,
                "_verify_add_phone_value_before_submit",
                side_effect=lambda _driver, phone: (
                    calls.append("verify_phone")
                    or {"visibleValue": "350 3568165", "hiddenValue": phone}
                ),
            ),
        ):
            roxy_codex_oauth._prepare_phone_submission(driver, "+573503568165")

        self.assertLess(calls.index("select_sms"), calls.index("type_phone"))
        self.assertLess(calls.index("type_phone"), calls.index("verify_phone"))

    def test_phone_input_is_typed_with_webdriver_keystrokes(self):
        class _Input:
            def __init__(self):
                self.calls = []

            def click(self):
                self.calls.append(call.click())

            def send_keys(self, *keys):
                self.calls.append(call.send_keys(*keys))

        phone_input = _Input()

        roxy_codex_oauth._type_phone_input_element(phone_input, "3142885896")

        self.assertEqual(phone_input.calls[0], call.click())
        self.assertEqual(phone_input.calls[-1], call.send_keys("3142885896"))
        self.assertGreaterEqual(len(phone_input.calls), 4)

    def test_phone_required_message_is_not_misclassified_as_whatsapp(self):
        state = {
            "url": "https://auth.openai.com/add-phone",
            "radios": [
                {"value": "sms", "checked": False},
                {"value": "whatsapp", "checked": True},
            ],
            "bodyText": "WhatsApp\n전화번호 필수\n계속하려면 전화번호를 추가하세요.",
        }

        self.assertEqual(
            roxy_codex_oauth._classify_phone_page_failure(state),
            "phone_required",
        )

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
        class _Input:
            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def click(self):
                pass

            def send_keys(self, *keys):
                pass

        class _Driver:
            def __init__(self):
                self.fill_args = None
                self.phone_input = _Input()

            def execute_script(self, script, *args):
                if not args and "ariaInvalid:" in script:
                    return {
                        "actualVisible": "7529516135",
                        "hiddenValue": "+447529516135",
                    }
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

            def find_elements(self, by, selector):
                return [self.phone_input]

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
