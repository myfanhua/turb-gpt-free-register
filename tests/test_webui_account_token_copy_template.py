# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class AccountTokenCopyTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_copy_tokens_prefers_selected_accounts(self):
        self.assertIn(
            "const selectedIds = Array.from(ACCOUNT_SELECTED).map(Number);",
            self.html,
        )
        self.assertIn("selectedIds.length ? selectedIds", self.html)

    def test_copy_tokens_falls_back_to_current_page_and_reports_count(self):
        self.assertIn(
            "ACCOUNTS.filter(r => r.has_access_token).map(r => Number(r.id))",
            self.html,
        )
        self.assertIn("已复制 ${tokens.length} 个 Token", self.html)


if __name__ == "__main__":
    unittest.main()
