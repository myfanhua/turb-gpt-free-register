# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class WebUiCodexRetryPlusGateTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1]
            / "webui"
            / "templates"
            / "index.html"
        ).read_text(encoding="utf-8")
        cls.single_retry = cls._section(
            "const btn = e.target.closest('[data-codex-retry]');",
            "$('#accountsBody').addEventListener('change'",
        )
        cls.bulk_retry = cls._section(
            "async function retrySelectedCodex()",
            "$('#qAccounts').addEventListener('input'",
        )

    @classmethod
    def _section(cls, start, end):
        start_index = cls.html.index(start)
        end_index = cls.html.index(end, start_index)
        return cls.html[start_index:end_index]

    def test_single_retry_confirmation_explains_plus_gate(self):
        self.assertIn("点击确定表示：我确认账号当前已实际开通 Plus", self.single_retry)
        self.assertIn(
            "实时查到非 Plus 仍停止",
            self.single_retry,
        )
        self.assertIn("查询失败按本次确认继续", self.single_retry)
        self.assertIn("plus_confirmed:true", self.single_retry)

    def test_bulk_retry_confirmation_explains_per_account_plus_gate(self):
        self.assertIn(
            "点击确定表示：我确认这些账号当前均已实际开通 Plus",
            self.bulk_retry,
        )
        self.assertIn("实时查到非 Plus 仍停止", self.bulk_retry)
        self.assertIn("查询失败按本次确认继续", self.bulk_retry)
        self.assertIn("plus_confirmed:true", self.bulk_retry)


if __name__ == "__main__":
    unittest.main()
