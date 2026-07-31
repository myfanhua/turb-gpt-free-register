# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from unittest.mock import patch

from webui.config_editor import EDITABLE_FIELDS, get_config


class ICloudConfigTests(unittest.TestCase):
    def test_email_config_declares_pickup_defaults(self):
        self.assertEqual(
            email.ICLOUD_PICKUP_API_BASE,
            "https://icloud.flysms.top/icloud/api/pickup",
        )
        self.assertEqual(email.ICLOUD_PICKUP_TIMEOUT, 15)
        self.assertEqual(
            email.ICLOUD_PROFILE_API_BASE,
            "https://icloud.flysms.top/icloud/api",
        )

    def test_email_config_env_override_registry_contains_pickup_fields(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn("'ICLOUD_PICKUP_API_BASE': 'str'", source)
        self.assertIn("'ICLOUD_PICKUP_TIMEOUT': 'int'", source)
        self.assertIn("'ICLOUD_PROFILE_API_BASE': 'str'", source)
        self.assertIn("'ICLOUD_PROFILE_TOKEN': 'str'", source)

    def test_webui_exposes_pickup_base_and_timeout(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["ICLOUD_PICKUP_API_BASE"]["group"], "邮箱 / OTP")
        self.assertEqual(fields["ICLOUD_PICKUP_TIMEOUT"]["type"], "int")
        self.assertEqual(fields["ICLOUD_PROFILE_TOKEN"]["group"], "邮箱 / OTP")
        self.assertTrue(fields["ICLOUD_PROFILE_TOKEN"]["secret"])

    @patch("config.env_loader.load_env")
    @patch(
        "config.env_loader.read_env_file",
        return_value={"ICLOUD_PROFILE_TOKEN": "profile_secret_1234"},
    )
    def test_webui_masks_profile_token_value(self, _read_env, _load_env):
        fields = {item["key"]: item for item in get_config()}

        self.assertEqual(fields["ICLOUD_PROFILE_TOKEN"]["value"], "")
        self.assertTrue(fields["ICLOUD_PROFILE_TOKEN"]["configured"])


if __name__ == "__main__":
    unittest.main()
