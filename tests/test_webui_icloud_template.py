# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class ICloudTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_import_and_pool_selects_include_icloud_api(self):
        self.assertGreaterEqual(self.html.count('value="icloud_api"'), 2)
        self.assertIn("邮箱 + Token", self.html)
        self.assertIn("邮箱 + 独立取件 URL", self.html)
        self.assertIn("三个及以上横线", self.html)

    def test_pool_label_and_masked_token_field_exist(self):
        self.assertIn("icloud_api:'iCloud API'", self.html)
        self.assertIn("r.token_masked", self.html)

    def test_pickup_mode_labels_are_rendered_without_raw_url(self):
        self.assertIn("function icloudPickupLabel(mode)", self.html)
        self.assertIn("api_token: 'API Token'", self.html)
        self.assertIn("independent_url: '独立 URL'", self.html)
        self.assertIn("independent_url_with_token: 'URL + API 后备'", self.html)
        self.assertNotIn("r.pickup_url", self.html)

    def test_icloud_import_never_uses_registered_account_mode(self):
        self.assertIn("source === 'icloud_api' ? false", self.html)

    def test_all_invalid_import_keeps_text_for_correction(self):
        self.assertIn("(r.inserted || 0) + (r.updated || 0) > 0", self.html)


if __name__ == "__main__":
    unittest.main()
