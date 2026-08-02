# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlsplit

from core import icloud_pickup_page as page


class ICloudPickupPageTests(unittest.TestCase):
    def test_with_message_limit_adds_limit_and_preserves_query(self):
        result = page.with_message_limit(
            "https://pickup.example/show/credential/one@icloud.com?view=compact#ignored",
            limit=10,
        )

        parsed = urlsplit(result)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "pickup.example")
        self.assertEqual(parsed.path, "/show/credential/one@icloud.com")
        self.assertEqual(parse_qs(parsed.query), {"view": ["compact"], "n": ["10"]})
        self.assertEqual(parsed.fragment, "")

    def test_with_message_limit_preserves_duplicate_query_parameters(self):
        result = page.with_message_limit(
            "https://pickup.example/show/one@icloud.com?scope=mail&scope=otp&n=3",
            limit=10,
        )

        self.assertEqual(
            parse_qs(urlsplit(result).query),
            {"scope": ["mail", "otp"], "n": ["10"]},
        )

    def test_empty_page_returns_no_messages(self):
        html = """
        <!doctype html><html><body>
          <div class="hd"><h1>one@icloud.com</h1><p>最新邮件</p></div>
          <div class="cnt">0 封</div>
          <div class="no"><p>等待接收邮件...</p></div>
        </body></html>
        """

        self.assertEqual(page.parse_pickup_page(html), [])

    def test_parses_nested_message_content_and_entities(self):
        html = """
        <html><body><div class="cnt">1 封</div>
          <div class="card">
            <div class="fr">ChatGPT &lt;noreply@tm.openai.com&gt;</div>
            <div class="su">Verify &amp; continue</div>
            <div class="dt">2026-08-02T06:00:00Z</div>
            <div class="bd"><p>Your verification code is <strong>654321</strong>.</p></div>
          </div>
        </body></html>
        """

        self.assertEqual(page.parse_pickup_page(html), [{
            "from": "ChatGPT <noreply@tm.openai.com>",
            "subject": "Verify & continue",
            "date": "2026-08-02T06:00:00Z",
            "body": "Your verification code is 654321.",
        }])

    def test_multiple_cards_remain_separate(self):
        html = """
        <div class="cnt">2 封</div>
        <div class="card"><div class="fr">first@example.com</div><div class="su">First</div>
          <div class="dt">2026-08-02T06:00:00Z</div><div class="bd">Body one</div></div>
        <div class="card"><div class="fr">second@example.com</div><div class="su">Second</div>
          <div class="dt">2026-08-02T06:01:00Z</div><div class="bd">Body two</div></div>
        """

        messages = page.parse_pickup_page(html)

        self.assertEqual([item["subject"] for item in messages], ["First", "Second"])
        self.assertEqual([item["body"] for item in messages], ["Body one", "Body two"])

    def test_unrecognized_page_raises_clear_error(self):
        with self.assertRaisesRegex(page.ICloudPickupPageError, "结构无法识别"):
            page.parse_pickup_page("<html><body><h1>Service unavailable</h1></body></html>")

    def test_expected_mailbox_rejects_page_for_another_mailbox(self):
        html = """
        <html><head><title>two@icloud.com</title></head><body>
          <div class="cnt">1 封</div>
          <div class="card"><div class="su">OpenAI code 654321</div>
            <div class="dt">2026-08-02T06:00:00Z</div><div class="bd">Code 654321</div></div>
        </body></html>
        """

        with self.assertRaisesRegex(page.ICloudPickupPageError, "页面邮箱不匹配"):
            page.parse_pickup_page(html, expected_email="one@icloud.com")

    def test_expected_mailbox_rejects_card_recipient_for_another_mailbox(self):
        html = """
        <html><head><title>one@icloud.com</title></head><body>
          <div class="cnt">1 封</div>
          <div class="card"><div class="to">notone@icloud.com</div>
            <div class="su">OpenAI code 654321</div>
            <div class="dt">2026-08-02T06:00:00Z</div><div class="bd">Code 654321</div></div>
        </body></html>
        """

        with self.assertRaisesRegex(page.ICloudPickupPageError, "邮件收件人不匹配"):
            page.parse_pickup_page(html, expected_email="one@icloud.com")

    def test_expected_mailbox_accepts_matching_h1_when_title_is_absent(self):
        html = """
        <html><body><div class="hd"><h1>one@icloud.com</h1></div>
          <div class="cnt">0 封</div><div class="no">等待接收邮件</div>
        </body></html>
        """

        self.assertEqual(
            page.parse_pickup_page(html, expected_email="one@icloud.com"),
            [],
        )

    def test_expected_mailbox_rejects_page_without_mailbox_identity(self):
        html = '<div class="cnt">0 封</div><div class="no">等待接收邮件</div>'

        with self.assertRaisesRegex(page.ICloudPickupPageError, "缺少邮箱标识"):
            page.parse_pickup_page(html, expected_email="one@icloud.com")


if __name__ == "__main__":
    unittest.main()
