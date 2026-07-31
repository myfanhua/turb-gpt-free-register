# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class ICloudTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_import_and_pool_selects_include_icloud_api(self):
        self.assertGreaterEqual(self.html.count('value="icloud_api"'), 2)
        self.assertIn("iCloud API: email----Token", self.html)

    def test_pool_label_and_masked_token_field_exist(self):
        self.assertIn("icloud_api:'iCloud API'", self.html)
        self.assertIn("r.token_masked", self.html)

    def test_icloud_import_never_uses_registered_account_mode(self):
        self.assertIn("source === 'icloud_api' ? false", self.html)


if __name__ == "__main__":
    unittest.main()
