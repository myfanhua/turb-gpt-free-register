import json
import unittest
from unittest.mock import Mock, patch

import requests

from config import roxybrowser as roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient


def _response(status_code: int, payload: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


class RoxyApiRetryTests(unittest.TestCase):
    def test_create_retries_when_roxy_reports_upstream_503_before_creation(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="token")
        client.http.request = Mock(side_effect=[
            _response(200, {"code": 500, "msg": "Request failed with status code 503"}),
            _response(200, {"code": 0, "data": {"dirId": "profile-1"}}),
        ])

        with patch.object(roxy_cfg, "ROXY_API_RETRIES", 3), \
             patch.object(roxy_cfg, "ROXY_API_RETRY_DELAY", 1), \
             patch("core.roxybrowser_client.time.sleep") as sleep:
            result = client.request("POST", "/browser/create", json_body={"workspaceId": 1})

        self.assertEqual(result["data"]["dirId"], "profile-1")
        self.assertEqual(client.http.request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_create_does_not_retry_ambiguous_timeout(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="token")
        client.http.request = Mock(side_effect=requests.Timeout("timed out"))

        with patch.object(roxy_cfg, "ROXY_API_RETRIES", 3), \
             patch("core.roxybrowser_client.time.sleep") as sleep:
            with self.assertRaises(requests.Timeout):
                client.request("POST", "/browser/create", json_body={"workspaceId": 1})

        self.assertEqual(client.http.request.call_count, 1)
        sleep.assert_not_called()

    def test_create_retries_when_roxy_is_still_finishing_another_creation(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="token")
        client.http.request = Mock(side_effect=[
            _response(200, {"code": 500, "msg": "正在创建中，请稍等！"}),
            _response(200, {"code": 0, "data": {"dirId": "profile-2"}}),
        ])

        with patch.object(roxy_cfg, "ROXY_API_RETRIES", 3), \
             patch.object(roxy_cfg, "ROXY_API_RETRY_DELAY", 1), \
             patch("core.roxybrowser_client.time.sleep") as sleep:
            result = client.request("POST", "/browser/create", json_body={"workspaceId": 1})

        self.assertEqual(result["data"]["dirId"], "profile-2")
        self.assertEqual(client.http.request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_create_does_not_retry_non_transient_quota_error(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="token")
        client.http.request = Mock(return_value=_response(200, {"code": 500, "msg": "窗口额度不足"}))

        with patch.object(roxy_cfg, "ROXY_API_RETRIES", 3), \
             patch("core.roxybrowser_client.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "窗口额度不足"):
                client.request("POST", "/browser/create", json_body={"workspaceId": 1})

        self.assertEqual(client.http.request.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
