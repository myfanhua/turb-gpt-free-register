# -*- coding: utf-8 -*-
import unittest


class ReadinessReportTests(unittest.TestCase):
    def _base_settings(self):
        return {
            "WEBUI_AUTH_CONFIGURED": True,
            "REGISTRATION_DRIVER": "cloak",
            "EMAIL_SOURCE": "outlook",
            "PROXY_PROVIDER": "manual",
            "PROXY_POOL_CONFIGURED": True,
        }

    def test_ready_report_requires_every_blocking_check_to_pass(self):
        from webui.readiness import build_readiness_report

        report = build_readiness_report(
            self._base_settings(),
            pool={"available": 3},
            storage_writable=True,
        )

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["counts"], {"ready": 5, "warning": 0, "blocked": 0})

    def test_generated_auth_code_is_a_warning_not_a_false_ready_state(self):
        from webui.readiness import build_readiness_report

        settings = self._base_settings()
        settings["WEBUI_AUTH_CONFIGURED"] = False
        report = build_readiness_report(settings, pool={"available": 2}, storage_writable=True)

        auth = next(item for item in report["checks"] if item["id"] == "webui_auth")
        self.assertEqual(auth["status"], "warning")
        self.assertEqual(report["status"], "warning")
        self.assertLess(report["score"], 100)

    def test_explicitly_disabled_auth_is_reported_accurately(self):
        from webui.readiness import build_readiness_report

        settings = self._base_settings()
        settings.update({"WEBUI_AUTH_CONFIGURED": False, "WEBUI_AUTH_DISABLED": True})
        report = build_readiness_report(settings, pool={"available": 2}, storage_writable=True)

        auth = next(item for item in report["checks"] if item["id"] == "webui_auth")
        self.assertEqual(auth["status"], "ready")
        self.assertIn("已关闭", auth["message"])

    def test_missing_selected_email_provider_credentials_blocks_readiness(self):
        from webui.readiness import build_readiness_report

        settings = self._base_settings()
        settings.update({"EMAIL_SOURCE": "gptmail", "GPTMAIL_API_KEY_CONFIGURED": False})
        report = build_readiness_report(settings, pool={"available": 0}, storage_writable=True)

        email = next(item for item in report["checks"] if item["id"] == "email_source")
        self.assertEqual(email["status"], "blocked")
        self.assertIn("GPTMail", email["message"])
        self.assertEqual(report["status"], "blocked")

    def test_dynamic_proxy_requires_account_password_host_and_valid_port(self):
        from webui.readiness import build_readiness_report

        settings = self._base_settings()
        settings.update({
            "PROXY_PROVIDER": "cliproxy_traffic",
            "CLIPROXY_PROXY_USERNAME_CONFIGURED": True,
            "CLIPROXY_PROXY_PASSWORD_CONFIGURED": False,
            "CLIPROXY_FORWARD_HOST": "us.cliproxy.io",
            "CLIPROXY_FORWARD_PORT": 3000,
        })
        report = build_readiness_report(settings, pool={"available": 1}, storage_writable=True)

        proxy = next(item for item in report["checks"] if item["id"] == "proxy_provider")
        self.assertEqual(proxy["status"], "blocked")
        self.assertIn("密码", proxy["message"])

    def test_roxy_local_api_token_is_optional(self):
        from webui.readiness import build_readiness_report

        settings = self._base_settings()
        settings.update({
            "REGISTRATION_DRIVER": "roxy",
            "ROXY_API_BASE": "http://127.0.0.1:50000",
            "ROXY_API_TOKEN_CONFIGURED": False,
            "ROXY_WORKSPACE_ID": "workspace-id",
            "ROXY_PROJECT_ID": "project-id",
        })
        report = build_readiness_report(settings, pool={"available": 1}, storage_writable=True)

        driver = next(item for item in report["checks"] if item["id"] == "registration_driver")
        self.assertEqual(driver["status"], "ready")
        self.assertNotIn("API Token", driver["message"])

    def test_report_never_echoes_secret_values(self):
        from webui.readiness import build_readiness_report

        secret = "never-render-this-secret"
        settings = self._base_settings()
        settings.update({
            "PROXY_PROVIDER": "1024proxy_traffic",
            "PROXY_1024_USERNAME": secret,
            "PROXY_1024_PASSWORD": secret,
            "PROXY_1024_USERNAME_CONFIGURED": True,
            "PROXY_1024_PASSWORD_CONFIGURED": True,
            "PROXY_1024_FORWARD_HOST": "proxy.example",
            "PROXY_1024_FORWARD_PORT": 3010,
        })
        report = build_readiness_report(settings, pool={"available": 1}, storage_writable=True)

        self.assertNotIn(secret, repr(report))


class ReadinessEndpointTests(unittest.TestCase):
    def test_authenticated_readiness_endpoint_returns_secret_free_contract(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        client = app.test_client()
        response = client.get(
            "/api/system/readiness",
            headers={"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn(payload["status"], {"ready", "warning", "blocked"})
        self.assertIsInstance(payload["score"], int)
        self.assertTrue(payload["checks"])
        for check in payload["checks"]:
            self.assertEqual(
                set(check),
                {"id", "label", "status", "message", "target_tab", "target_group"},
            )
        rendered = repr(payload).lower()
        self.assertNotIn("password", rendered)
        self.assertNotIn("api_token", rendered)


if __name__ == "__main__":
    unittest.main()
