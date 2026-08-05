# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class WebUiAccessTokenRecoveryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_toolbar_has_recover_and_stop_buttons(self):
        self.assertIn('id="btnRecoverSelectedAccessTokens"', self.html)
        self.assertIn('id="btnStopSelectedAccessTokenRecovery"', self.html)

    def test_account_row_has_single_recover_stop_and_log_actions(self):
        self.assertIn("data-at-recover", self.html)
        self.assertIn("data-at-recovery-stop", self.html)
        self.assertIn("data-at-recovery-log", self.html)

    def test_polling_merges_recovery_state(self):
        self.assertIn("wasRecovering", self.html)
        self.assertIn("isRecovering", self.html)
        self.assertIn("at_recovery_status", self.html)

    def test_bulk_calls_expected_endpoints(self):
        self.assertIn("/api/accounts/recover-access-token-bulk", self.html)
        self.assertIn("/api/accounts/recover-access-token/stop-bulk", self.html)

    def test_toolbar_and_rows_describe_invalid_token_recovery(self):
        self.assertIn(">重补AT</button>", self.html)
        self.assertIn("AT失效", self.html)
        self.assertIn("重补AT", self.html)
        self.assertNotIn(">补缺失AT</button>", self.html)

    def test_single_and_bulk_requests_send_force_mode(self):
        self.assertIn("force: force", self.html)
        self.assertIn("JSON.stringify({account_ids: ids, force: true})", self.html)

    def test_http_401_accounts_use_automatic_eligibility(self):
        self.assertIn(
            "const force = account.has_access_token && !account.access_token_invalid",
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
