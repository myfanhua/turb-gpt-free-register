# -*- coding: utf-8 -*-
import unittest
from unittest.mock import call, patch

from webui.app import create_app


class WebUiPlanCheckProxyTests(unittest.TestCase):
    def setUp(self):
        with patch("webui.app.db.recover_interrupted_extract_links", return_value={
            "failed_count": 0,
            "kakao_batches": [],
        }), patch(
            "webui.app.extract_link_service.resume_interrupted_kakao_batches",
            return_value={"resumed_batches": 0, "failed_batches": 0},
            create=True,
        ):
            self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @staticmethod
    def _account(account_id, email, proxy):
        return {
            "id": account_id,
            "email": email,
            "access_token": f"token-{account_id}",
            "proxy_used": proxy,
        }

    def test_single_check_defaults_to_account_registration_proxy(self):
        account = self._account(7, "single@example.com", "http://sid-7:bridge@127.0.0.1:25001")
        with patch("webui.app.db.get_account", return_value=account), patch(
            "webui.app.plan_check_service.enqueue_account_plan_check",
            return_value={"accepted": True, "busy": False},
        ) as enqueue:
            response = self.client.post("/api/accounts/check-plan", json={"account_id": 7})

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once_with(
            account_id=7,
            email="single@example.com",
            access_token="token-7",
            trigger="manual",
            proxy="http://sid-7:bridge@127.0.0.1:25001",
            timezone_offset_min="-",
        )

    def test_single_check_explicit_proxy_overrides_account_proxy(self):
        account = self._account(7, "single@example.com", "http://stored-proxy")
        with patch("webui.app.db.get_account", return_value=account), patch(
            "webui.app.plan_check_service.enqueue_account_plan_check",
            return_value={"accepted": True, "busy": False},
        ) as enqueue:
            response = self.client.post("/api/accounts/check-plan", json={
                "account_id": 7,
                "proxy": "http://explicit-proxy",
            })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["proxy"], "http://explicit-proxy")

    def test_bulk_check_defaults_to_each_accounts_registration_proxy(self):
        first = self._account(11, "first@example.com", "http://sid-11:bridge@127.0.0.1:25001")
        second = self._account(12, "second@example.com", "http://sid-12:bridge@127.0.0.1:25001")
        accounts = {11: first, 12: second}
        with patch("webui.app.db.get_account", side_effect=lambda account_id: accounts[account_id]), patch(
            "webui.app.plan_check_service.enqueue_account_plan_check",
            return_value={"accepted": True, "busy": False},
        ) as enqueue:
            response = self.client.post("/api/accounts/check-plan-bulk", json={
                "account_ids": [11, 12],
            })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args_list, [
            call(
                account_id=11,
                email="first@example.com",
                access_token="token-11",
                trigger="manual_bulk",
                proxy="http://sid-11:bridge@127.0.0.1:25001",
                timezone_offset_min="-",
            ),
            call(
                account_id=12,
                email="second@example.com",
                access_token="token-12",
                trigger="manual_bulk",
                proxy="http://sid-12:bridge@127.0.0.1:25001",
                timezone_offset_min="-",
            ),
        ])

    def test_bulk_check_explicit_proxy_overrides_every_account_proxy(self):
        accounts = {
            11: self._account(11, "first@example.com", "http://stored-11"),
            12: self._account(12, "second@example.com", "http://stored-12"),
        }
        with patch("webui.app.db.get_account", side_effect=lambda account_id: accounts[account_id]), patch(
            "webui.app.plan_check_service.enqueue_account_plan_check",
            return_value={"accepted": True, "busy": False},
        ) as enqueue:
            response = self.client.post("/api/accounts/check-plan-bulk", json={
                "account_ids": [11, 12],
                "proxy": "http://explicit-proxy",
            })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            [item.kwargs["proxy"] for item in enqueue.call_args_list],
            ["http://explicit-proxy", "http://explicit-proxy"],
        )


if __name__ == "__main__":
    unittest.main()
