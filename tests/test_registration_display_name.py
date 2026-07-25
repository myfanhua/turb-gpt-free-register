# -*- coding: utf-8 -*-
import re
import unittest
from unittest.mock import patch

import main
from config import register as register_cfg
from core import registration_service
from core.profile_utils import generate_display_name


class RegistrationDisplayNameTests(unittest.TestCase):
    def test_default_ja_name_is_ascii_romanized_two_words(self):
        name = generate_display_name()
        self.assertRegex(name, r"^[A-Za-z]+ [A-Za-z]+$")

    def test_unsupported_locale_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "REGISTER_NAME_LOCALE"):
            generate_display_name("zh")

    def test_cli_and_web_service_use_the_same_ja_generator(self):
        expected = "Haruto Sato"
        with patch.object(main, "REGISTER_EMAIL", "person@example.test"), \
             patch.object(main, "REGISTER_NAME", ""), \
             patch.object(main._email_cfg, "USE_EMAIL_SERVICE", True), \
             patch.object(main, "generate_display_name", return_value=expected) as cli_generate:
            _, name, _ = main.prepare_registration_inputs()
        self.assertEqual(name, expected)
        cli_generate.assert_called_once_with(register_cfg.REGISTER_NAME_LOCALE)

        with patch("core.profile_utils.generate_display_name", return_value=expected) as web_generate:
            self.assertEqual(registration_service._random_display_name(), expected)
        web_generate.assert_called_once_with(register_cfg.REGISTER_NAME_LOCALE)


if __name__ == "__main__":
    unittest.main()
