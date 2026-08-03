import unittest
from pathlib import Path


class WebUiExtractLinkProviderTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1]
            / "webui"
            / "templates"
            / "index.html"
        ).read_text(encoding="utf-8")

    def test_toolbar_contains_provider_batch_and_default_controls(self):
        self.assertIn('id="extractLinkProvider"', self.html)
        self.assertIn('id="extractLinkBatchSize"', self.html)
        self.assertIn('id="btnSaveExtractDefaults"', self.html)
        self.assertIn('min="1" max="5"', self.html)

    def test_start_requests_send_current_provider_and_batch_size(self):
        self.assertIn("provider: currentExtractProvider()", self.html)
        self.assertIn("batch_size: currentExtractBatchSize()", self.html)
        self.assertIn("/api/extract-link/options", self.html)
        self.assertIn("/api/extract-link/defaults", self.html)

    def test_extract_config_has_three_subsections(self):
        self.assertIn("CONFIG_EXTRACT_ACTIVE_SECTION", self.html)
        self.assertIn("extractConfigSectionForKey", self.html)
        self.assertIn("['通用配置', '旧接口', 'Kakao API']", self.html)

    def test_kakao_proxy_pool_switch_defaults_on(self):
        from config import extract_link
        from webui.config_editor import EDITABLE_FIELDS

        field = next(
            item
            for item in EDITABLE_FIELDS
            if item.get("key") == "KAKAO_EXTRACT_USE_PROXY_POOL"
        )
        self.assertTrue(extract_link.KAKAO_EXTRACT_USE_PROXY_POOL)
        self.assertEqual(field["type"], "bool")
        self.assertEqual(field["group"], "提链")

    def test_status_rendering_mentions_selected_provider(self):
        self.assertIn("extract_link_provider", self.html)
        self.assertIn("Kakao", self.html)

    def test_manual_extract_treats_local_trial_flag_as_advisory(self):
        self.assertIn("function isExtractableFreeAccount", self.html)
        self.assertIn("本地未检测到 Plus 试用资格", self.html)


if __name__ == "__main__":
    unittest.main()
