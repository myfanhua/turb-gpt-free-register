# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from webui.config_editor import EDITABLE_FIELDS


class ICloudConfigTests(unittest.TestCase):
    def test_email_config_declares_pickup_defaults(self):
        self.assertEqual(
            email.ICLOUD_PICKUP_API_BASE,
            "https://icloud.flysms.top/icloud/api/pickup",
        )
        self.assertEqual(email.ICLOUD_PICKUP_TIMEOUT, 15)

    def test_email_config_env_override_registry_contains_pickup_fields(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn("'ICLOUD_PICKUP_API_BASE': 'str'", source)
        self.assertIn("'ICLOUD_PICKUP_TIMEOUT': 'int'", source)

    def test_webui_exposes_pickup_base_and_timeout(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["ICLOUD_PICKUP_API_BASE"]["group"], "邮箱 / OTP")
        self.assertEqual(fields["ICLOUD_PICKUP_TIMEOUT"]["type"], "int")


if __name__ == "__main__":
    unittest.main()
