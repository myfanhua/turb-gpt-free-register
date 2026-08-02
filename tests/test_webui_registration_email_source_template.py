# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class RegistrationEmailSourceTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_registration_form_contains_all_source_choices(self):
        selector = self.template.split('id="regEmailSource"', 1)[1].split("</select>", 1)[0]
        for expected in (
            'id="regEmailSource"',
            "跟随当前配置",
            "iCloud 全部",
            "iCloud API",
            "iCloud 独立 URL",
        ):
            self.assertIn(expected, self.template)
        self.assertNotIn("URL + API 后备", selector)

    def test_registration_page_loads_metadata_and_submits_selected_source(self):
        self.assertIn("/api/email-sources", self.template)
        self.assertIn("email_source: selectedSource", self.template)
        self.assertIn("loadRegistrationEmailSources()", self.template)

    def test_temporary_registration_selection_is_not_persisted(self):
        self.assertNotIn("localStorage.setItem('regEmailSource'", self.template)
        self.assertNotIn('localStorage.setItem("regEmailSource"', self.template)

    def test_source_hint_distinguishes_batch_selection_from_default_config(self):
        self.assertIn("当前默认配置", self.template)
        self.assertIn("本次选择", self.template)
        self.assertIn("configured_label", self.template)


if __name__ == "__main__":
    unittest.main()
