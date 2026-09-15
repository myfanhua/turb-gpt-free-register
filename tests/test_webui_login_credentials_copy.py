# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiLoginCredentialsCopyTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.get_account")
    def test_secret_endpoint_returns_email_registration_password_and_totp(self, get_account):
        get_account.return_value = {
            "id": 41,
            "email": "user@example.test",
            "extra_json": '{"registration_password": "account-password"}',
            "totp_secret": "TOTPSECRET",
        }

        response = self.client.get("/api/accounts/41/secret?field=login_credentials")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["value"],
            "user@example.test---account-password---TOTPSECRET",
        )

    @patch("webui.app.db.get_account")
    def test_secret_endpoint_keeps_empty_password_and_totp_positions(self, get_account):
        get_account.return_value = {
            "id": 42,
            "email": "legacy@example.test",
            "totp_secret": "",
        }

        response = self.client.get("/api/accounts/42/secret?field=login_credentials")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["value"], "legacy@example.test------")


if __name__ == "__main__":
    unittest.main()
