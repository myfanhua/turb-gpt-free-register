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

    def test_account_table_renders_registration_location(self):
        self.assertIn("col-location", self.html)
        self.assertIn("<th>注册地址</th>", self.html)
        self.assertIn("function _registrationLocationCell(r)", self.html)
        self.assertIn("r.registration_country_code", self.html)
        self.assertIn("r.registration_country", self.html)
        self.assertIn("r.registration_region", self.html)
        self.assertIn("r.registration_ip", self.html)
        self.assertIn("<td>${_registrationLocationCell(r)}</td>", self.html)
        self.assertIn('colspan="13" class="muted">暂无账号', self.html)


if __name__ == "__main__":
    unittest.main()
