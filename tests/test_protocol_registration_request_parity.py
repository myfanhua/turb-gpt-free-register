"""目标工程条件请求的契约回归；无线上访问。"""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from core import chatgpt_auth, protocol_registration as flow
from tests.test_protocol_registration_alignment import Response, session


class SelectionRequestParityTests(unittest.TestCase):
    sid = 'us_1234567890abcdefghijkl'
    referer = 'https://auth.openai.com/choose-an-account'

    def select(self, s):
        return flow.select_registration_account(s, referer=self.referer, html=self.sid)

    def test_missing_json_endpoint_uses_form_action(self):
        for status in (404, 405):
            with self.subTest(status=status):
                s = session()
                s.post.side_effect = [Response(status), Response(303, headers={'Location': '/authorize/continue'})]
                self.assertEqual(self.select(s), 'https://auth.openai.com/authorize/continue')
                first, fallback = s.post.call_args_list
                self.assertEqual(json.loads(first.kwargs['data']), {'session_id': self.sid})
                self.assertEqual(first.kwargs['headers']['origin'], 'https://auth.openai.com')
                self.assertEqual(fallback.args[0], self.referer)
                self.assertEqual(parse_qs(fallback.kwargs['data']), {'intent': ['select'], 'session_id': [self.sid]})
                self.assertEqual(fallback.kwargs['headers']['content-type'], 'application/x-www-form-urlencoded')
                self.assertEqual(fallback.kwargs['headers']['origin'], 'https://auth.openai.com')
                for call in s.post.call_args_list:
                    self.assertFalse(call.kwargs['allow_redirects'])

    def test_unknown_or_rejected_selection_does_not_fallback(self):
        for failure in (Response(401), Response(403), Response(409), Response(429), Response(500),
                        Response(data={'success': False}), TimeoutError('network timeout')):
            with self.subTest(failure=failure):
                s = session()
                s.post.side_effect = [failure]
                with self.assertRaises(Exception):
                    self.select(s)
                s.post.assert_called_once()

    def test_redirect_is_returned_without_consuming_callback(self):
        s = session()
        callback = 'https://chatgpt.com/api/auth/callback/openai?code=c&state=s'
        s.post.return_value = Response(302, headers={'Location': callback})
        self.assertEqual(self.select(s), callback)
        s.get.assert_not_called()

    def test_redirect_requires_valid_location(self):
        for headers in ({}, {'Location': 'https://example.com/callback'}):
            s = session()
            s.post.return_value = Response(303, headers=headers)
            with self.assertRaises(RuntimeError):
                self.select(s)
            s.post.assert_called_once()

    def test_form_fallback_can_return_json_next_step(self):
        s = session()
        s.post.side_effect = [Response(405), Response(data={'continue_url': '/authorize/continue'})]
        self.assertEqual(self.select(s), 'https://auth.openai.com/authorize/continue')

    def test_successful_selection_is_not_repeated_after_next_hop_timeout(self):
        s = session()
        s.get.side_effect = [Response(text=self.sid), TimeoutError('network'),
                             Response(302, headers={'Location': 'https://chatgpt.com/'}), Response()]
        s.post.return_value = Response(data={'continue_url': '/authorize/continue'})
        with self.assertRaises(TimeoutError):
            flow.follow_registration_callback(s, self.referer, 'https://auth.openai.com/')
        self.assertEqual(flow.follow_registration_callback(s, self.referer, 'https://auth.openai.com/'),
                         'https://chatgpt.com/')
        s.post.assert_called_once()

    def test_unknown_selection_result_only_reads_page_no_post_replay(self):
        s = session()
        s.get.return_value = Response(text=self.sid)
        s.post.side_effect = TimeoutError('network')
        with self.assertRaises(TimeoutError):
            flow.follow_registration_callback(s, self.referer, 'https://auth.openai.com/')
        with self.assertRaisesRegex(RuntimeError, '账号选择后仍返回'):
            flow.follow_registration_callback(s, self.referer, 'https://auth.openai.com/')
        s.post.assert_called_once()


class ReauthorizationRequestParityTests(unittest.TestCase):
    def test_reauthorization_callback_is_consumed_once_by_state_machine(self):
        s = session()
        authorize = 'https://auth.openai.com/authorize?state=s&prompt=login'
        callback = 'https://chatgpt.com/api/auth/callback/openai?code=c&state=s'
        s.get.side_effect = [Response(302, headers={'Location': callback}),
                             Response(302, headers={'Location': '/'}), Response()]
        with patch.object(chatgpt_auth, 'get_csrf_token', return_value='csrf'), \
             patch.object(chatgpt_auth, 'signin_openai', return_value=authorize):
            start = flow.reauthorize_registration_session(s, 'a@example.com')
        s.get.assert_not_called()
        self.assertEqual(flow.follow_registration_callback(s, start, 'https://auth.openai.com/about-you'),
                         'https://chatgpt.com/')
        urls = [call.args[0] for call in s.get.call_args_list]
        self.assertEqual(urls.count(callback), 1)
        self.assertNotIn('prompt=', urls[0])
        self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in s.get.call_args_list))
