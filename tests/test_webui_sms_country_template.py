# -*- coding: utf-8 -*-
import re
import unittest
from pathlib import Path


class WebUiSmsCountryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1]
            / "webui"
            / "templates"
            / "index.html"
        ).read_text(encoding="utf-8")

    def test_preferred_country_field_has_a_custom_searchable_renderer(self):
        self.assertIn("function renderSmsPreferredCountriesField", self.html)
        self.assertIn("f.key === 'SMS_PREFERRED_COUNTRIES'", self.html)
        self.assertIn('id="smsCountrySearch"', self.html)
        self.assertIn('id="smsCountryOptions"', self.html)
        self.assertIn("sms-country-chip", self.html)
        self.assertIn("sms-country-option", self.html)

    def test_picker_keeps_existing_multiline_save_contract(self):
        self.assertRegex(
            self.html,
            re.compile(
                r'<textarea[^>]+data-key="SMS_PREFERRED_COUNTRIES"[^>]+hidden',
                re.S,
            ),
        )
        self.assertIn(
            "CONFIG_PENDING_UPDATES.SMS_PREFERRED_COUNTRIES = next;",
            self.html,
        )
        self.assertIn("id=\"btnSaveConfig\"", self.html)
        self.assertIn("body: JSON.stringify({updates})", self.html)

    def test_catalog_has_explicit_loading_error_and_retry_state(self):
        for expected in (
            "SMS_COUNTRY_CATALOG",
            "SMS_COUNTRY_CATALOG_ERROR",
            "SMS_COUNTRY_CATALOG_LOADING",
            "SMS_COUNTRY_CATALOG_ATTEMPTED",
            "SMS_COUNTRY_CATALOG_LOADED",
            "/api/sms/countries",
            "data-sms-country-refresh",
        ):
            self.assertIn(expected, self.html)
        self.assertIn("bindSmsCountryPicker();", self.html)

    def test_country_search_and_unknown_saved_codes_are_supported(self):
        self.assertIn("country.name", self.html)
        self.assertIn("country.code", self.html)
        self.assertIn("toLocaleLowerCase", self.html)
        self.assertIn("未在国家目录中", self.html)
        self.assertIn("data-sms-country-remove", self.html)

    def test_common_sms_section_contains_country_routing_keys(self):
        section_start = self.html.index("function smsConfigSectionForKey")
        section_end = self.html.index("function codexConfigSectionForKey", section_start)
        section = self.html[section_start:section_end]
        self.assertIn("SMS_PREFERRED_COUNTRIES", section)
        self.assertIn("SMS_COUNTRY_FAILURE_SWITCH", section)

    def test_picker_css_is_compact_responsive_and_keyboard_visible(self):
        for expected in (
            ".sms-country-field",
            ".sms-country-chips",
            ".sms-country-options",
            ".sms-country-option",
            ":focus-visible",
            "flex-wrap: wrap",
            "@media (max-width: 760px)",
        ):
            self.assertIn(expected, self.html)


if __name__ == "__main__":
    unittest.main()
