# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import live_check_service


class LiveCheckServiceTests(unittest.TestCase):
    def _common_patches(self, account):
        return (
            patch.object(live_check_service, "check_account_plan", create=True),
            patch.object(live_check_service, "check_account_liveness"),
            patch.object(live_check_service, "resolve_plan_check_route"),
            patch.object(live_check_service, "_append_log"),
            patch.object(live_check_service, "_QUEUE_SLOTS"),
            patch.object(live_check_service.db, "get_account", return_value=account),
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "update_account_liveness"),
            patch.object(
                live_check_service,
                "resolve_account_plan_proxy",
                create=True,
                return_value=(None, None),
            ),
        )

    def test_saved_access_token_is_checked_before_mailbox_login(self):
        account = {"id": 8, "access_token": "stored-at"}
        patches = self._common_patches(account)
        with patches[0] as check_plan, patches[1] as login_check, patches[2], patches[3], patches[4] as slots, patches[5], patches[6], patches[7] as update, patches[8]:
            check_plan.return_value = {
                "ok": True,
                "checked_at": "2026-08-29T23:00:00",
                "http_status": 200,
                "network_route": "direct_fallback",
                "proxy_used": "socks5h://***:***@proxy.example:3000",
            }
            login_check.side_effect = AssertionError("有效 AT 时不应重新登录邮箱")

            result = live_check_service._run_live_check(
                account_id=8,
                email="account@example.test",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["source"], "stored_at")
        check_plan.assert_called_once_with("stored-at", proxy=None)
        login_check.assert_not_called()
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1]["source"], "stored_at")
        slots.release.assert_called_once()

    def test_saved_access_token_reuses_registration_proxy_context(self):
        account = {"id": 8, "access_token": "stored-at"}
        patches = self._common_patches(account)
        with patches[0] as check_plan, patches[1] as login_check, patches[2], patches[3], patches[4] as slots, patches[5], patches[6], patches[7], patches[8], patch.object(
            live_check_service,
            "resolve_account_plan_proxy",
            create=True,
            return_value=("jp-proxy", {"proxy_country": "JP"}),
        ) as resolve_proxy:
            check_plan.return_value = {
                "ok": True,
                "checked_at": "2026-08-29T23:00:00",
                "http_status": 200,
            }
            login_check.side_effect = AssertionError("有效 AT 时不应重新登录邮箱")

            result = live_check_service._run_live_check(
                account_id=8,
                email="account@example.test",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        resolve_proxy.assert_called_once_with(
            account_id=8,
            email="account@example.test",
            proxy=None,
        )
        check_plan.assert_called_once_with("stored-at", proxy="jp-proxy")
        slots.release.assert_called_once()

    def test_expired_saved_token_falls_back_to_full_login(self):
        account = {"id": 9, "access_token": "expired-at"}
        patches = self._common_patches(account)
        with patches[0] as check_plan, patches[1] as login_check, patches[2] as resolve_route, patches[3], patches[4] as slots, patches[5], patches[6], patches[7], patches[8]:
            check_plan.return_value = {
                "ok": False,
                "checked_at": "2026-08-29T23:00:00",
                "http_status": 401,
                "token_expired": True,
                "needs_live_check": True,
                "error": "AT已过期/失效，请手动查活刷新",
            }
            resolve_route.return_value = {
                "proxy": "",
                "proxy_mode": "auto",
                "network_route": "direct_fallback",
                "proxy_used": None,
                "proxy_fallback_reason": "proxy unavailable",
            }
            login_check.return_value = {
                "ok": True,
                "status": "live",
                "checked_at": "2026-08-29T23:00:01",
                "access_token": "fresh-at",
            }

            result = live_check_service._run_live_check(
                account_id=9,
                email="account@example.test",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        check_plan.assert_called_once_with("expired-at", proxy=None)
        login_check.assert_called_once_with("account@example.test", proxy="", clear_log=False)
        slots.release.assert_called_once()

    def test_auto_live_route_falls_back_direct_on_proxy_transport_failure(self):
        account = {"id": 10, "access_token": ""}
        patches = self._common_patches(account)
        with patches[0] as check_plan, patches[1] as login_check, patches[2] as resolve_route, patches[3], patches[4] as slots, patches[5], patches[6], patches[7], patches[8]:
            resolve_route.return_value = {
                "proxy": "socks5h://user:pass@proxy.example:3000",
                "proxy_mode": "auto",
                "network_route": "proxy",
                "proxy_used": "socks5h://***:***@proxy.example:3000",
                "proxy_fallback_reason": None,
            }
            login_check.side_effect = [
                {"ok": False, "status": "failed", "error": "ProxyError: proxy closed connection"},
                {"ok": True, "status": "live", "checked_at": "2026-08-29T23:00:02", "access_token": "fresh-at"},
            ]

            result = live_check_service._run_live_check(
                account_id=10,
                email="account@example.test",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        check_plan.assert_not_called()
        self.assertEqual(
            [call.kwargs["proxy"] for call in login_check.call_args_list],
            ["socks5h://user:pass@proxy.example:3000", ""],
        )
        slots.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
