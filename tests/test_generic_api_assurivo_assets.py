# -*- coding: utf-8 -*-
"""generic_api 中完整 Assurivo 查询 URL 的受控资产回归。"""
import json
import unittest
from unittest.mock import patch

from core import email_provider
from core.generic_api_mail_client import GenericApiEmailAccount, _CONTEXT_CACHE
from webui.app import create_app


class GenericApiAssurivoAssetTests(unittest.TestCase):
    def setUp(self):
        _CONTEXT_CACHE.clear()
        self.url = "https://assurivo.com/console/open.php?mail=a%40test.com&pwd=a%26b&limit=5"
        _CONTEXT_CACHE["a@test.com"] = GenericApiEmailAccount("a@test.com", self.url)

    def tearDown(self):
        _CONTEXT_CACHE.clear()

    @patch("core.email_provider.resolve_email_source", return_value="generic_api")
    def test_asset_keeps_query_url_without_fabricating_credential(self, _source):
        asset = email_provider.get_email_asset_context("a@test.com")
        self.assertEqual(asset["provider"], "generic_api")
        self.assertEqual(asset["query_url"], self.url)
        self.assertNotIn("email_credential", asset)
        self.assertTrue(asset["exportable"])

    @patch("webui.app.db.get_account")
    def test_explicit_query_endpoint_allows_generic_url_only_in_response(self, get_account):
        get_account.return_value = {"id": 9, "extra_json": json.dumps({"email_asset": {"provider": "generic_api", "query_url": self.url}})}
        response = create_app(auth_code="test-auth").test_client().post(
            "/api/accounts/9/email-asset-query", headers={"X-Auth-Code": "test-auth"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["query_url"], self.url)

    @patch("webui.app.db.get_account", return_value=None)
    def test_no_query_url_or_password_on_absent_account_error(self, _get_account):
        response = create_app(auth_code="test-auth").test_client().post(
            "/api/accounts/9/email-asset-query", headers={"X-Auth-Code": "test-auth"}
        )
        self.assertNotIn("pwd", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
