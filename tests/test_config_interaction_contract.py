# -*- coding: utf-8 -*-
import unittest

from webui.app import create_app


class ConfigInteractionContractTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def _html(self):
        response = self.client.get(
            "/",
            headers={"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_dynamic_config_rerenders_reapply_mode_search_and_active_nav(self):
        html = self._html()

        self.assertIn("function rerenderConfigLayoutV2", html)
        self.assertIn("filterConfigSections();", html)
        self.assertIn("setActiveConfigNav(activeTarget);", html)
        self.assertIn("CONFIG_CODEX_ACTIVE_SECTION_V2 = codexTab.dataset.codexSectionV2;\n        rerenderConfigLayoutV2();", html)
        self.assertIn("CONFIG_EMAIL_ACTIVE_SECTION_V2 = emailTab.dataset.emailSectionV2;\n        rerenderConfigLayoutV2();", html)
        self.assertIn("CONFIG_SMS_ACTIVE_SECTION_V2 = smsTab.dataset.smsSectionV2;\n        rerenderConfigLayoutV2();", html)

    def test_roxy_workspace_save_keeps_feedback_and_syncs_fields_without_full_rerender(self):
        html = self._html()
        start = html.index("async function saveRoxyWorkspaceSelection()")
        end = html.index("function readConfigElementValue", start)
        body = html[start:end]

        self.assertIn("syncRoxyWorkspaceSaveStateV2", html)
        self.assertIn("syncSavedConfigValueV2('ROXY_WORKSPACE_ID', workspaceId)", body)
        self.assertIn("syncSavedConfigValueV2('ROXY_PROJECT_ID', projectId)", body)
        self.assertNotIn("renderConfigLayoutV2();", body)

    def test_cloudmail_actions_restore_success_feedback_after_config_reload(self):
        html = self._html()

        self.assertIn("freshCloudMailToolStateV2", html)
        self.assertIn("await loadConfig();\n    freshCloudMailToolStateV2", html)


if __name__ == "__main__":
    unittest.main()
