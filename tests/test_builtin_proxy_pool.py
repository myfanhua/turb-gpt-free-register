# -*- coding: utf-8 -*-
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit


class BuiltinProxyPoolTests(unittest.TestCase):
    def test_provider_import_has_no_config_proxy_cycle(self):
        """直接导入 provider 时不能因 config.proxy 的启动导入顺序失败。"""
        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-c", "import core.proxy_provider; print('provider-import-ok')"],
            cwd=project_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provider-import-ok", result.stdout)

    def test_cliproxy_uses_configured_default_country(self):
        from config import proxy as proxy_cfg
        from core.proxy_provider import build_proxy

        with patch.object(proxy_cfg, "CLIPROXY_PROXY_USERNAME", "fixture-user"), \
                patch.object(proxy_cfg, "CLIPROXY_PROXY_PASSWORD", "fixture-pass"), \
                patch.object(proxy_cfg, "CLIPROXY_FORWARD_HOST", "proxy.example.test"), \
                patch.object(proxy_cfg, "CLIPROXY_FORWARD_PORT", 3128), \
                patch.object(proxy_cfg, "CLIPROXY_STICKY_MINUTES", 60), \
                patch.object(proxy_cfg, "CLIPROXY_PROXY_COUNTRY", "ID", create=True):
            value = build_proxy("cliproxy_traffic", None)

        parsed = urlsplit(value)
        self.assertEqual(parsed.scheme, "socks5h")
        self.assertEqual(parsed.hostname, "proxy.example.test")
        self.assertEqual(parsed.port, 3128)
        self.assertTrue(parsed.username.startswith("fixture-user-region-ID-sid-"))

    def test_pick_proxy_routes_selected_provider_to_builtin_builder(self):
        from config import proxy as proxy_cfg

        with patch.object(proxy_cfg, "PROXY_PROVIDER", "cliproxy_traffic"), \
                patch.object(proxy_cfg, "CLIPROXY_PROXY_COUNTRY", "ID", create=True), \
                patch("core.proxy_provider.build_proxy", return_value="builtin://selected") as builder:
            value = proxy_cfg.pick_proxy()

        self.assertEqual(value, "builtin://selected")
        builder.assert_called_once_with("cliproxy_traffic", "ID")

    def test_proxy_editor_exposes_cliproxy_country(self):
        from webui.config_editor import EDITABLE_FIELDS

        field = next(item for item in EDITABLE_FIELDS if item["key"] == "CLIPROXY_PROXY_COUNTRY")
        self.assertEqual(field["file"], "proxy.py")
        self.assertEqual(field["type"], "str")

    def test_iproyal_uses_country_and_sticky_session_in_password(self):
        from config import proxy as proxy_cfg
        from core.proxy_provider import build_proxy

        with patch.object(proxy_cfg, "IPROYAL_PROXY_USERNAME", "fixture-user", create=True), \
                patch.object(proxy_cfg, "IPROYAL_PROXY_PASSWORD", "fixture-pass", create=True), \
                patch.object(proxy_cfg, "IPROYAL_FORWARD_HOST", "geo.example.test", create=True), \
                patch.object(proxy_cfg, "IPROYAL_FORWARD_PORT", 12321, create=True), \
                patch.object(proxy_cfg, "IPROYAL_STICKY_MINUTES", 60, create=True), \
                patch.object(proxy_cfg, "IPROYAL_PROXY_COUNTRY", "ID", create=True):
            value = build_proxy("iproyal_traffic", None)

        parsed = urlsplit(value)
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.hostname, "geo.example.test")
        self.assertEqual(parsed.port, 12321)
        self.assertEqual(parsed.username, "fixture-user")
        self.assertTrue(parsed.password.startswith("fixture-pass_country-id_session-"))
        self.assertTrue(parsed.password.endswith("_lifetime-60m"))

    def test_proxy_editor_hides_manual_pool_and_exposes_both_builtin_platforms(self):
        from webui.config_editor import EDITABLE_FIELDS

        keys = {item["key"] for item in EDITABLE_FIELDS}
        self.assertNotIn("PROXY_POOL", keys)
        self.assertIn("IPROYAL_PROXY_USERNAME", keys)
        self.assertIn("IPROYAL_PROXY_PASSWORD", keys)
        self.assertIn("IPROYAL_PROXY_COUNTRY", keys)

    def test_provider_catalog_contains_cli_and_iproyal_without_secrets(self):
        from core.proxy_provider import provider_catalog

        catalog = provider_catalog()
        self.assertEqual(
            {item["value"] for item in catalog},
            {"cliproxy_traffic", "iproyal_traffic"},
        )
        rendered = repr(catalog)
        self.assertNotIn("CLIPROXY_PROXY_PASSWORD", rendered)
        self.assertNotIn("IPROYAL_PROXY_PASSWORD", rendered)

    def test_job_row_carries_proxy_selection(self):
        from core import db

        row = db._new_job_row(
            [],
            email_source="fixture",
            proxy_provider="iproyal_traffic",
            proxy_country="ID",
        )
        self.assertEqual(row["proxy_provider"], "iproyal_traffic")
        self.assertEqual(row["proxy_country"], "ID")

    def test_settings_from_values_uses_provider_defaults_for_optional_fields(self):
        from config import proxy as proxy_cfg
        from core.proxy_provider import settings_from_values

        with patch.object(proxy_cfg, "IPROYAL_FORWARD_HOST", "geo.example.test", create=True), \
                patch.object(proxy_cfg, "IPROYAL_FORWARD_PORT", 12321, create=True), \
                patch.object(proxy_cfg, "IPROYAL_STICKY_MINUTES", 60, create=True), \
                patch.object(proxy_cfg, "IPROYAL_PROXY_COUNTRY", "ID", create=True):
            values = settings_from_values(
                "iproyal_traffic",
                {"username": "fixture-user", "password": "fixture-pass", "country": "ID"},
            )

        self.assertEqual(values["IPROYAL_FORWARD_HOST"], "geo.example.test")
        self.assertEqual(values["IPROYAL_FORWARD_PORT"], 12321)
        self.assertEqual(values["IPROYAL_STICKY_MINUTES"], 60)

    def test_settings_from_values_keeps_saved_credentials_when_form_is_blank(self):
        from config import proxy as proxy_cfg
        from core.proxy_provider import settings_from_values

        with patch.object(proxy_cfg, "IPROYAL_PROXY_USERNAME", "saved-user", create=True), \
                patch.object(proxy_cfg, "IPROYAL_PROXY_PASSWORD", "saved-pass", create=True):
            values = settings_from_values(
                "iproyal_traffic",
                {"username": "", "password": "", "country": "ID"},
            )

        self.assertEqual(values["IPROYAL_PROXY_USERNAME"], "saved-user")
        self.assertEqual(values["IPROYAL_PROXY_PASSWORD"], "saved-pass")

    def test_config_endpoint_masks_saved_secret_values(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        response = app.test_client().get(
            "/api/config",
            headers={"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"},
        )

        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        secret_rows = [row for row in rows if row.get("secret")]
        self.assertTrue(secret_rows)
        self.assertTrue(all(row.get("value", "") == "" for row in secret_rows))
        self.assertTrue(all("configured" in row for row in secret_rows))

    def test_proxy_options_endpoint_is_secret_free_and_exposes_platforms(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        response = app.test_client().get(
            "/api/proxy/options",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            {item["value"] for item in payload["providers"]},
            {"cliproxy_traffic", "iproyal_traffic"},
        )
        self.assertTrue(payload["countries"])
        rendered = repr(payload)
        self.assertNotIn("PROXY_PASSWORD", rendered)
        self.assertNotIn("PROXY_USERNAME", rendered)

    def test_provider_country_catalog_is_provider_specific_and_expanded(self):
        from core.proxy_provider import country_catalog_for_provider

        cli = {item["value"] for item in country_catalog_for_provider("cliproxy_traffic")}
        royal = {item["value"] for item in country_catalog_for_provider("iproyal_traffic")}
        self.assertTrue({"ID", "US", "JP", "PH"}.issubset(cli))
        self.assertTrue({"ID", "US", "JP", "PH"}.issubset(royal))
        self.assertGreater(len(cli), 13)
        self.assertGreater(len(royal), 13)

    def test_proxy_test_route_uses_defaults_and_only_saves_after_success(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        with patch("core.proxy_provider.test_proxy_connection", return_value={"ok": True, "status_code": 204}), \
                patch("config.env_loader.write_env_values", return_value=["IPROYAL_PROXY_USERNAME", "IPROYAL_PROXY_PASSWORD"]), \
                patch("config.reload_all", return_value=[]):
            response = app.test_client().post(
                "/api/proxy/test",
                headers={"X-Auth-Code": "test-auth", "Content-Type": "application/json"},
                json={
                    "provider": "iproyal_traffic",
                    "country": "ID",
                    "values": {"username": "fixture-user", "password": "fixture-pass", "country": "ID"},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["endpoint"], {"host": "geo.iproyal.com", "port": 12321})
        self.assertEqual(payload["status_code"], 204)
        self.assertNotIn("fixture-pass", repr(payload))

    def test_config_update_does_not_clear_saved_secret_when_form_is_blank(self):
        from webui import config_editor

        with patch("config.env_loader.read_env_file", return_value={"CLIPROXY_PROXY_PASSWORD": "saved-secret"}), \
                patch("config.env_loader.write_env_values", return_value=["CLIPROXY_FORWARD_PORT"]) as write_env, \
                patch("config.env_loader.load_env"):
            result = config_editor.update_config({
                "CLIPROXY_PROXY_PASSWORD": "",
                "CLIPROXY_FORWARD_PORT": 3010,
            })

        write_env.assert_called_once_with({"CLIPROXY_FORWARD_PORT": "3010"})
        self.assertEqual(result["env_updated"], ["CLIPROXY_FORWARD_PORT"])


if __name__ == "__main__":
    unittest.main()
