"""匿名预热请求范围回归。"""
import unittest
from unittest.mock import MagicMock

from core.chatgpt_bootstrap import anonymous_bootstrap


class AnonymousBootstrapTests(unittest.TestCase):
    def test_keeps_initialization_without_requesting_pricing(self):
        for profile in ({'geo': {'country': 'HK'}}, {'navigator_language': 'ja-JP'}, {}):
            for strict in (False, True):
                with self.subTest(profile=profile, strict=strict):
                    session = MagicMock()
                    session.browser_profile = profile
                    session.js_timezone_offset_min.return_value = -480
                    session.get.return_value.status_code = 200
                    anonymous_bootstrap(session, strict=strict)
                    self.assertEqual([call.args[0] for call in session.get.call_args_list], [
                        'https://chatgpt.com/backend-anon/accounts/check/v4-2023-04-27?timezone_offset_min=-480',
                        'https://chatgpt.com/ces/v1/projects/oai/settings',
                        'https://chatgpt.com/backend-anon/me',
                    ])
                    session.post.assert_not_called()
                    self.assertEqual(session.reset_circuit_breaker.call_count, 0 if strict else 1)
                    session.log_cookie_names.assert_called_once_with('anonymous_bootstrap_complete')


if __name__ == '__main__':
    unittest.main()
