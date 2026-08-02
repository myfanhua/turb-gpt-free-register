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

    def test_registration_launch_controls_use_aligned_scoped_grid(self):
        registration_card = self.template.split("启动批量注册", 1)[1].split(
            "任务列表", 1
        )[0]

        self.assertIn('class="registration-launch-grid"', registration_card)
        self.assertIn('class="registration-source-field fld"', registration_card)
        self.assertIn(
            'class="registration-source-hint hint" id="regEmailSourceHint"',
            registration_card,
        )
        source_label = registration_card.split('id="regEmailSource"', 1)[1].split(
            "</label>", 1
        )[0]
        self.assertNotIn('id="regEmailSourceHint"', source_label)
        self.assertIn(
            "grid-template-columns: minmax(260px, 1.25fr) minmax(160px, 1fr) "
            "minmax(160px, 1fr) auto;",
            self.template,
        )
        self.assertIn(
            "@media (max-width: 760px)",
            self.template,
        )


if __name__ == "__main__":
    unittest.main()
