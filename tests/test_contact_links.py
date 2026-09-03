# -*- coding: utf-8 -*-
import unittest

from webui.app import create_app


class ContactLinksRenderTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def _html(self, ui):
        response = self.client.get(
            f"/?ui={ui}",
            headers={"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_modern_sidebar_renders_contact_line_and_two_external_links(self):
        html = self._html("modern")

        self.assertIn('class="sidebar-contact"', html)
        self.assertIn("ai交流资源群", html)
        self.assertIn(
            'href="https://t.me/mzaijlq" target="_blank" rel="noopener noreferrer"',
            html,
        )
        self.assertIn(
            'href="https://qm.qq.com/cgi-bin/qm/qr?group_code=952990450" target="_blank" rel="noopener noreferrer"',
            html,
        )
        self.assertIn("QQ群：952990450", html)
        self.assertIn('aria-label="Telegram 交流群"', html)
        self.assertIn('aria-label="QQ 交流群 952990450"', html)

    def test_legacy_header_keeps_contact_line_and_two_external_links(self):
        html = self._html("legacy")

        self.assertIn('class="contact-links"', html)
        self.assertIn("ai交流资源群", html)
        self.assertIn("QQ群：952990450", html)
        self.assertIn('href="https://t.me/mzaijlq"', html)
        self.assertIn('href="https://qm.qq.com/cgi-bin/qm/qr?group_code=952990450"', html)


if __name__ == "__main__":
    unittest.main()
