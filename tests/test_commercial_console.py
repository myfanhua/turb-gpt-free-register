# -*- coding: utf-8 -*-
import unittest

from webui.app import create_app


class CommercialConsoleRenderTests(unittest.TestCase):
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

    def test_workbench_is_the_default_supported_console_entry(self):
        html = self._html()

        self.assertIn('data-tab="dashboard" class="sidebar-item active"', html)
        self.assertIn('id="tab-dashboard"', html)
        self.assertIn('id="tab-register" class="hidden"', html)
        self.assertIn("'/api/system/readiness'", html)
        self.assertIn("function loadWorkbench()", html)

    def test_console_has_accessible_navigation_and_live_readiness_feedback(self):
        html = self._html()

        self.assertIn('class="skip-link" href="#main-content"', html)
        self.assertIn('<main id="main-content"', html)
        self.assertIn('id="readinessLive" aria-live="polite"', html)
        self.assertIn('aria-label="主导航"', html)

    def test_legacy_ui_is_not_advertised_in_primary_navigation(self):
        html = self._html()

        self.assertNotIn('href="/?ui=legacy"', html)
        legacy = self.client.get(
            "/?ui=legacy",
            headers={"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"},
        )
        self.assertEqual(legacy.status_code, 200)

    def test_account_bulk_actions_use_progressive_disclosure(self):
        html = self._html()

        self.assertIn('class="account-actions-primary"', html)
        self.assertIn('class="account-actions-advanced"', html)
        self.assertIn('<summary>更多批量操作', html)
        self.assertIn('id="btnCheckSelectedLiveV2"', html)
        self.assertIn('id="btnRetrySelectedCodexV2"', html)
        self.assertIn('id="btnDeleteSelectedAccountsV2"', html)

    def test_account_table_shows_registration_country_next_to_note(self):
        html = self._html()

        self.assertIn(
            '<th class="col-note">备注</th>\n              <th class="col-country">注册国家</th>',
            html,
        )
        self.assertIn("r.registration_proxy_country", html)
        self.assertIn('colspan="13"', html)

    def test_account_txt_download_uses_fixed_four_field_format(self):
        html = self._html()

        self.assertIn("账号----密码----2FA----API地址", html)
        self.assertIn("fetchAccountSecrets(ids, 'download_txt_line')", html)

    def test_settings_support_basic_advanced_modes_and_search(self):
        html = self._html()

        self.assertIn('id="configSearchV2"', html)
        self.assertIn('data-config-mode="basic"', html)
        self.assertIn('data-config-mode="advanced"', html)
        self.assertIn('id="configFilterStatusV2" aria-live="polite"', html)
        self.assertIn('function filterConfigSections()', html)

    def test_settings_show_all_groups_by_default(self):
        html = self._html()

        self.assertIn("let CONFIG_DISPLAY_MODE_V2 = 'advanced';", html)
        self.assertIn('class="is-active" data-config-mode="advanced" aria-pressed="true"', html)
        self.assertIn('data-config-mode="basic" aria-pressed="false"', html)
        self.assertIn('id="configModeLabelV2">高级</div>', html)
        self.assertIn('bindConfigLayoutV2();\n    filterConfigSections();', html)

    def test_date_filters_use_icons_instead_of_emoji(self):
        html = self._html()

        self.assertNotIn('📅', html)
        self.assertIn('class="date-filter-icon"', html)

    def test_builtin_proxy_controls_are_visible_in_settings_and_task_center(self):
        html = self._html()

        for token in (
            "内置代理",
            "regProxyProviderV2",
            "regProxyCountryV2",
            "/api/proxy/options",
            "/api/proxy/test",
            'data-proxy-test-save="cliproxy_traffic"',
            'data-proxy-test-save="iproyal_traffic"',
        ):
            self.assertIn(token, html)

    def test_trial_payment_probe_controls_and_labels_are_visible(self):
        html = self._html()

        self.assertIn('id="btnCheckSelectedTrialPaymentsV2"', html)
        self.assertIn("/api/accounts/check-trial-payments-bulk", html)
        self.assertIn("Momo", html)
        self.assertIn("PayPal", html)
        self.assertIn("GoPay", html)

    def test_task_log_surfaces_final_job_error(self):
        html = self._html()

        self.assertIn("r.job && r.job.error_message", html)
        self.assertIn("任务失败：", html)


if __name__ == "__main__":
    unittest.main()
