import re
import unittest
from pathlib import Path


class AccountRegistrationLocationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1]
            / "webui"
            / "templates"
            / "index.html"
        ).read_text(encoding="utf-8")

    def test_location_column_follows_source_in_table_contract(self):
        self.assertIn(
            '<col class="col-source"><col class="col-location"><col class="col-token">',
            self.html,
        )
        self.assertIn(
            '<th>来源</th><th>注册地址</th><th>Token</th>',
            self.html,
        )
        self.assertIn('.accounts-table .col-location { width: 140px; }', self.html)

    def test_overflow_columns_shift_with_inserted_location_column(self):
        selectors = re.findall(
            r"\.accounts-table td:nth-child\((\d+)\), "
            r"\.accounts-table th:nth-child\(\1\) "
            r"\{ overflow: visible; text-overflow: clip; \}",
            self.html,
        )
        self.assertEqual(selectors, ["7", "11", "9", "12"])

    def test_renderer_localizes_country_and_keeps_fallback_fields(self):
        self.assertIn(
            "new Intl.DisplayNames(['zh-CN'], {type: 'region'})",
            self.html,
        )
        self.assertIn("r.registration_country_code", self.html)
        self.assertIn("r.registration_country", self.html)
        self.assertIn("return fallback || code;", self.html)

    def test_renderer_formats_location_lines_tooltip_and_empty_value(self):
        renderer = self.html.split("function _registrationLocationCell(r)", 1)[1].split(
            "function renderAccounts()", 1
        )[0]
        self.assertIn("r.registration_region", renderer)
        self.assertIn("r.registration_ip", renderer)
        self.assertIn("[country, region, ip].filter(Boolean).join(' · ')", renderer)
        self.assertIn('<div class="main-cell">${esc(primary)}</div>', renderer)
        self.assertIn('<div class="sub-cell mono">${esc(ip)}</div>', renderer)
        self.assertIn("return '<span class=\"muted\">-</span>';", renderer)

    def test_account_rows_and_empty_state_match_thirteen_columns(self):
        self.assertIn("<td>${_registrationLocationCell(r)}</td>", self.html)
        self.assertIn('colspan="13" class="muted">暂无账号', self.html)


if __name__ == "__main__":
    unittest.main()
