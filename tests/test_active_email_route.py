# -*- coding: utf-8 -*-
import unittest

from config import env_loader


class ActiveEmailRouteTests(unittest.TestCase):
    def test_active_registration_route_is_worker_only_and_not_qq(self):
        values = env_loader.read_env_file()
        sources = [item.strip() for item in values.get("EMAIL_SOURCE", "").split(",") if item.strip()]

        self.assertEqual(sources, ["cloudflare"])
        self.assertEqual(values.get("EMAIL_DOMAIN", "").strip(), "")
        self.assertEqual(
            values.get("CLOUDFLARE_API_BASE", "").rstrip("/"),
            "https://mzemail.fengshuieon.com",
        )
        self.assertEqual(values.get("CLOUDFLARE_AUTH_MODE", "").strip().lower(), "x-admin-auth")
        self.assertEqual(values.get("CLOUDFLARE_PATH_ACCOUNTS", "").strip(), "/admin/new_address")
        self.assertNotIn("cloudflare_domain", sources)


if __name__ == "__main__":
    unittest.main()
