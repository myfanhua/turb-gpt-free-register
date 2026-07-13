# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


class GPTMailConfigTests(unittest.TestCase):
    def test_email_config_declares_gptmail_api_key_with_empty_default(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn('GPTMAIL_API_KEY = env_str("GPTMAIL_API_KEY", "")', source)

    def test_secret_registry_includes_gptmail_api_key(self):
        self.assertEqual(SECRET_ENV_KEYS["GPTMAIL_API_KEY"], "GPTMail API Key")

    def test_webui_exposes_gptmail_key_as_secret_env_field(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "GPTMAIL_API_KEY")
        self.assertEqual(field["group"], "邮箱 / OTP")
        self.assertTrue(field["secret"])
        self.assertEqual(field["storage"], "env")
