import ast
import time
import unittest

import phonenumbers
from phonenumbers import geocoder as phone_geocoder
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "core" / "roxy_codex_oauth.py"


class _Driver:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.current_url = "https://auth.openai.com/u/add-phone"

    def execute_script(self, script, *args):
        self.calls.append((script, args))
        if not self.results:
            raise AssertionError("unexpected execute_script call")
        return self.results.pop(0)


def _load_phone_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    wanted = {
        "_is_phone_code_state",
        "_classify_phone_page_failure",
        "_phone_country_identity",
        "_select_phone_country",
        "_verify_add_phone_value_before_submit",
    }
    body = [
        node for node in tree.body
        if (
            isinstance(node, ast.FunctionDef) and node.name in wanted
        ) or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_PHONE_COUNTRY_NAME_ALIASES" for target in node.targets)
        )
    ]
    namespace = {
        "time": time,
        "phonenumbers": phonenumbers,
        "phone_geocoder": phone_geocoder,
        "_phone_page_state": lambda driver: {"url": getattr(driver, "current_url", "")},
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class RoxyCodexPhoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_phone_helpers()

    def classify(self, state):
        return self.helpers["_classify_phone_page_failure"](state)

    def test_sms_choice_is_not_mislabeled_by_whatsapp_page_text(self):
        state = {
            "url": "https://auth.openai.com/u/add-phone",
            "radios": [
                {"value": "sms", "checked": True},
                {"value": "whatsapp", "checked": False},
            ],
            "bodyText": "Choose SMS or WhatsApp to receive a verification code.",
        }
        self.assertEqual(self.classify(state), "")

    def test_phone_number_required_has_specific_classification(self):
        state = {
            "url": "https://auth.openai.com/u/add-phone",
            "radios": [
                {"value": "sms", "checked": True},
                {"value": "whatsapp", "checked": False},
            ],
            "bodyText": "WhatsApp  Phone number required",
        }
        self.assertEqual(self.classify(state), "phone_number_required")

    def test_checked_or_only_whatsapp_is_classified(self):
        checked = {
            "url": "https://auth.openai.com/u/add-phone",
            "radios": [
                {"value": "sms", "checked": False},
                {"value": "whatsapp", "checked": True},
            ],
        }
        only = {
            "url": "https://auth.openai.com/u/add-phone",
            "radios": [{"value": "whatsapp", "checked": False}],
        }
        self.assertEqual(self.classify(checked), "whatsapp_channel")
        self.assertEqual(self.classify(only), "whatsapp_channel")

    def test_phone_code_page_is_not_classified_as_send_failure(self):
        state = {
            "url": "https://auth.openai.com/u/phone-verification",
            "radios": [{"value": "whatsapp", "checked": True}],
            "bodyText": "Phone number required",
        }
        self.assertEqual(self.classify(state), "")

    def test_native_country_selection_is_confirmed_before_return(self):
        driver = _Driver([
            {
                "ok": True,
                "mode": "native_select",
                "dialCode": "84",
                "selectedText": "Vietnam (+84)",
                "selectedKey": "VN",
                "selectedChanged": True,
            },
            {"ok": True, "combined": "Vietnam (+84)", "codes": ["84"]},
        ])
        result = self.helpers["_select_phone_country"](driver, "+84833648563", timeout=1)
        self.assertEqual(result["dialCode"], "84")
        self.assertEqual(result["mode"], "native_select")
        self.assertEqual(result["region"], "VN")
        self.assertEqual(len(driver.calls), 2)
        first_args = driver.calls[0][1]
        self.assertEqual(first_args[0], "84833648563")
        self.assertEqual(first_args[1], "84")
        self.assertIn("Vietnam", first_args[2])
        self.assertIn("Viet Nam", first_args[2])

    def test_phone_identity_uses_full_e164_number(self):
        identity = self.helpers["_phone_country_identity"]("+84377221015")
        self.assertEqual(identity["e164"], "+84377221015")
        self.assertEqual(identity["dialCode"], "84")
        self.assertEqual(identity["region"], "VN")
        self.assertIn("Vietnam", identity["aliases"])
        self.assertIn("Viet Nam", identity["aliases"])

    def test_country_name_only_listbox_is_selected_and_confirmed(self):
        driver = _Driver([
            {"ok": False, "opened": True, "mode": "listbox"},
            {
                "mode": "listbox",
                "dialCode": "84",
                "selectedText": "Viet Nam",
                "selectedKey": "VN",
                "countryName": "Viet Nam",
                "selectedChanged": True,
            },
            {"ok": True, "combined": "Viet Nam VN", "codes": [], "matchedAlias": "Viet Nam"},
        ])
        result = self.helpers["_select_phone_country"](driver, "+84377221015", timeout=1)
        self.assertEqual(result["dialCode"], "84")
        self.assertEqual(result["countryName"], "Viet Nam")
        self.assertEqual(result["region"], "VN")
        self.assertEqual(len(driver.calls), 3)
        listbox_args = driver.calls[1][1]
        self.assertEqual(listbox_args[1], "84")
        self.assertIn("Vietnam", listbox_args[2])
        self.assertIn("Viet Nam", listbox_args[2])

    def test_submit_check_accepts_national_number_with_matching_country(self):
        driver = _Driver([{
            "ok": True,
            "countryOk": True,
            "visibleOk": True,
            "hiddenOk": True,
            "visibleValue": "833 648 563",
            "hiddenValue": "+84833648563",
            "dialCode": "84",
            "countryText": "Vietnam (+84)",
        }])
        result = self.helpers["_verify_add_phone_value_before_submit"](
            driver, "+84833648563", "84"
        )
        self.assertEqual(result["dialCode"], "84")
        submit_args = driver.calls[0][1]
        self.assertEqual(submit_args[0], "+84833648563")
        self.assertEqual(submit_args[1], "84")
        self.assertIn("Vietnam", submit_args[2])

    def test_submit_check_accepts_country_name_without_dial_code(self):
        driver = _Driver([{
            "ok": True,
            "countryOk": True,
            "visibleOk": True,
            "hiddenOk": True,
            "visibleValue": "377 221 015",
            "hiddenValue": "+84377221015",
            "dialCode": "84",
            "pageDialCode": "",
            "matchedAlias": "Vietnam",
            "countryText": "Vietnam VN",
        }])
        result = self.helpers["_verify_add_phone_value_before_submit"](
            driver, "+84377221015", "84"
        )
        self.assertTrue(result["countryOk"])
        self.assertEqual(driver.calls[0][1][1], "84")
        self.assertIn("Viet Nam", driver.calls[0][1][2])

    def test_submit_check_rejects_country_mismatch(self):
        driver = _Driver([{
            "ok": False,
            "countryOk": False,
            "visibleOk": True,
            "hiddenOk": True,
            "visibleValue": "833648563",
            "hiddenValue": "+84833648563",
            "dialCode": "1",
            "countryText": "United States (+1)",
        }])
        with self.assertRaisesRegex(RuntimeError, "phone_country_mismatch"):
            self.helpers["_verify_add_phone_value_before_submit"](
                driver, "+84833648563", "84"
            )

    def test_submit_check_rejects_reverted_phone_value(self):
        driver = _Driver([{
            "ok": False,
            "countryOk": True,
            "visibleOk": False,
            "hiddenOk": True,
            "visibleValue": "",
            "hiddenValue": "+84833648563",
            "dialCode": "84",
            "countryText": "Vietnam (+84)",
        }])
        with self.assertRaisesRegex(RuntimeError, "phone_value_mismatch"):
            self.helpers["_verify_add_phone_value_before_submit"](
                driver, "+84833648563", "84"
            )


if __name__ == "__main__":
    unittest.main()
