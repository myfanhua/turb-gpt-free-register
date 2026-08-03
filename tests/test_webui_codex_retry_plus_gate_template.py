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
        self.assertIn("先实时确认当前已开通 Plus", self.single_retry)
        self.assertIn(
            "确认 Plus 后才会消耗邮箱 OTP 和接码短信",
            self.single_retry,
        )
        self.assertIn("非 Plus 或查询失败会直接停止", self.single_retry)

    def test_bulk_retry_confirmation_explains_per_account_plus_gate(self):
        self.assertIn(
            "每个账号分别实时查询当前实际 Plus 状态",
            self.bulk_retry,
        )
        self.assertIn("只有实际 Plus 才继续", self.bulk_retry)
        self.assertIn(
            "确认 Plus 后才会消耗邮箱 OTP 和接码短信",
            self.bulk_retry,
        )
        self.assertIn("非 Plus 或查询失败直接停止", self.bulk_retry)


if __name__ == "__main__":
    unittest.main()
