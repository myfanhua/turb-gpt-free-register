# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, call

from core import roxy_codex_oauth, roxybrowser_client
from core.session import BrowserSession


class AccountDeviceContextTests(unittest.TestCase):
    def test_browser_session_reuses_supplied_device_id(self):
        device_id = "11111111-2222-4333-8444-555555555555"
        session = BrowserSession(proxy="", detect_exit_geo=False, device_id=device_id)

        self.assertEqual(session.device_id, device_id)
        cookie_values = {
            cookie.value
            for cookie in session.session.cookies.jar
            if cookie.name == "oai-did"
        }
        self.assertEqual(cookie_values, {device_id})
        session.session.close()

    def test_roxy_driver_receives_same_device_cookie_for_all_auth_origins(self):
        device_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"success": True}

        actual = roxybrowser_client.install_account_device_id(driver, device_id)

        self.assertEqual(actual, device_id)
        self.assertEqual(driver.execute_cdp_cmd.call_args_list, [
            call("Network.enable", {}),
            call("Network.setCookie", {
                "name": "oai-did",
                "value": device_id,
                "url": "https://chatgpt.com/",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }),
            call("Network.setCookie", {
                "name": "oai-did",
                "value": device_id,
                "url": "https://auth.openai.com/",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }),
            call("Network.setCookie", {
                "name": "oai-did",
                "value": device_id,
                "url": "https://sentinel.openai.com/",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }),
        ])
        self.assertEqual(driver._account_device_id, device_id)

    def test_roxy_driver_rejects_empty_device_id_before_navigation(self):
        driver = Mock()
        with self.assertRaisesRegex(ValueError, "device_id"):
            roxybrowser_client.install_account_device_id(driver, "  ")
        driver.execute_cdp_cmd.assert_not_called()

    def test_roxy_oauth_resolves_saved_device_id_for_account(self):
        device_id = "11111111-2222-4333-8444-555555555555"
        with unittest.mock.patch.object(
            roxy_codex_oauth.db,
            "get_account_by_email",
            return_value={"email": "user@example.com", "device_id": device_id},
        ):
            self.assertEqual(
                roxy_codex_oauth._resolve_account_device_id("user@example.com"),
                device_id,
            )

    def test_roxy_oauth_stops_before_navigation_when_device_id_is_missing(self):
        with unittest.mock.patch.object(
            roxy_codex_oauth.db,
            "get_account_by_email",
            return_value={"email": "old@example.com", "device_id": None},
        ):
            with self.assertRaisesRegex(RuntimeError, "device_id"):
                roxy_codex_oauth._resolve_account_device_id("old@example.com")

    def test_roxy_oauth_reinstalls_device_id_after_clearing_state(self):
        events = []
        driver = Mock()
        device_id = "11111111-2222-4333-8444-555555555555"
        with unittest.mock.patch.object(
            roxy_codex_oauth,
            "clear_roxy_browser_auth_state",
            side_effect=lambda _driver: events.append("clear"),
        ), unittest.mock.patch.object(
            roxy_codex_oauth,
            "install_account_device_id",
            side_effect=lambda _driver, value: events.append(f"install:{value}") or value,
        ):
            roxy_codex_oauth._prepare_account_device_context(
                driver,
                device_id,
                clear_existing_state=True,
            )

        self.assertEqual(events, ["clear", f"install:{device_id}"])


if __name__ == "__main__":
    unittest.main()
