# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import chatgpt_plan


class ChatGptPlanDeviceContextTests(unittest.TestCase):
    def test_plan_check_constructs_http_session_with_account_device_id(self):
        device_id = "11111111-2222-4333-8444-555555555555"
        response = Mock(
            status_code=200,
            text='{"accounts":{"default":{"account":{"account_id":"acc-1","plan_type":"free"},"entitlement":{}}}}',
        )
        response.json.return_value = {
            "accounts": {
                "default": {
                    "account": {"account_id": "acc-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        env = Mock()
        env.device_id = device_id
        env.navigator_language.return_value = "en-US"
        env._get_common_headers.return_value = {}
        env.session.get.return_value = response

        with patch.object(chatgpt_plan, "BrowserSession", return_value=env) as session_cls:
            result = chatgpt_plan.check_account_plan(
                "header.payload.signature",
                proxy="",
                device_id=device_id,
                max_attempts=1,
            )

        self.assertTrue(result["ok"])
        session_cls.assert_called_once_with(
            proxy="",
            detect_exit_geo=False,
            device_id=device_id,
        )


if __name__ == "__main__":
    unittest.main()
