"""注册后 inline 2FA 与认证恢复回归，不访问外部服务。"""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from core import account_export as export, chatgpt_auth, openai_auth, protocol_registration as flow
from core.registration_service import StopRequested
from tests.test_protocol_registration_alignment import Response, session


SECRET = 'JBSWY3DPEHPK3PXP'


class RegistrationInlineTwofaTests(unittest.TestCase):
    def make_session(self):
        s = session()
        s.device_id = 'device'
        s.navigator_language.return_value = 'en-US'
        s.get_chatgpt_headers.side_effect = lambda **kw: {}
        s.get.return_value = Response(data={'mfa_enabled': False, 'factors': {}})
        s.post.side_effect = [Response(data={'secret': SECRET, 'session_id': 'enroll-1', 'factor': {'id': 'factor-1'}}),
                              Response(data={'success': True})]
        return s

    def test_registration_inline_reuses_token_without_reauth(self):
        s = self.make_session()
        with patch.object(export, '_trigger_reauth_with_retry') as reauth, \
             patch('core.chatgpt_bootstrap.authenticated_bootstrap') as bootstrap:
            result = export.setup_2fa(s, 'a@example.com', access_token='token', prefer_inline=True)
        self.assertEqual(result, SECRET)
        reauth.assert_not_called()
        bootstrap.assert_not_called()
        self.assertEqual(s.post.call_count, 2)
        self.assertTrue(s.get.call_args.args[0].endswith('/accounts/mfa_info'))
        enroll, activate = s.post.call_args_list
        self.assertEqual(enroll.kwargs['headers']['authorization'], 'Bearer token')
        self.assertEqual(json.loads(enroll.kwargs['data']), {'factor_type': 'totp'})
        body = json.loads(activate.kwargs['data'])
        self.assertEqual(body['session_id'], 'enroll-1')
        self.assertEqual(body['factor_type'], 'totp')
        self.assertEqual(len(body['code']), 6)
        self.assertIsNone(s._pending_totp_enrollment)

    def test_authentication_insufficient_falls_back_to_existing_reauth(self):
        s = self.make_session()
        s.get.return_value = Response(401)
        with patch.object(export, '_trigger_reauth_with_retry', side_effect=RuntimeError('entered reauth')) as reauth, \
             patch('core.chatgpt_bootstrap.authenticated_bootstrap') as bootstrap, \
             patch.object(export, 'human_delay'):
            with self.assertRaisesRegex(RuntimeError, 'entered reauth'):
                export.setup_2fa(s, 'a@example.com', access_token='token', prefer_inline=True)
        reauth.assert_called_once()
        bootstrap.assert_not_called()
        s.post.assert_not_called()

    def test_enroll_401_allows_reauth_but_not_403_429_or_network_error(self):
        s = self.make_session()
        s.post.side_effect = [Response(401)]
        self.assertIsNone(export._try_inline_totp(s, 'token'))
        for failure in (Response(403), Response(429), TimeoutError('connection timed out')):
            s = self.make_session()
            s.post.side_effect = [failure]
            with self.assertRaises(Exception):
                export._try_inline_totp(s, 'token')
            self.assertEqual(s.post.call_count, 1)

    def test_already_enabled_factor_does_not_create_new_enrollment(self):
        for factor in ({'id': 'existing'}, True):
            s = self.make_session()
            s.get.return_value = Response(data={'mfa_enabled': True, 'factors': {'totp': factor}})
            with self.assertRaisesRegex(RuntimeError, '已启用 TOTP'):
                export._try_inline_totp(s, 'token')
            s.post.assert_not_called()

    def test_activation_timeout_confirms_exact_new_factor(self):
        s = self.make_session()
        s.get.side_effect = [Response(data={'mfa_enabled': False}),
                             Response(data={'mfa_enabled': True, 'factors': {'totp': {'id': 'factor-1'}}})]
        s.post.side_effect = [Response(data={'secret': SECRET, 'session_id': 'enroll-1', 'factor': {'id': 'factor-1'}}),
                              TimeoutError('connection timed out')]
        self.assertEqual(export._try_inline_totp(s, 'token'), SECRET)
        self.assertEqual(s.post.call_count, 2)
        self.assertIsNone(s._pending_totp_enrollment)

    def test_activation_mismatched_factor_retains_unconfirmed_recovery_material(self):
        s = self.make_session()
        s.get.side_effect = [Response(data={'mfa_enabled': False}),
                             Response(data={'mfa_enabled': True, 'factors': {'totp': {'id': 'other-factor'}}})]
        s.post.side_effect = [Response(data={'secret': SECRET, 'session_id': 'enroll-1', 'factor': {'id': 'factor-1'}}),
                              Response(401)]
        with self.assertRaisesRegex(RuntimeError, 'factor_id 不匹配'):
            export._try_inline_totp(s, 'token')
        self.assertEqual(s.post.call_count, 2)
        self.assertEqual(s._pending_totp_enrollment['secret'], SECRET)

    def test_activation_failure_without_factor_id_never_reenrolls(self):
        s = self.make_session()
        s.post.side_effect = [Response(data={'secret': SECRET, 'session_id': 'enroll-1'}), Response(data={'success': False})]
        with self.assertRaises(RuntimeError):
            export._try_inline_totp(s, 'token')
        self.assertEqual(s.post.call_count, 2)
        self.assertEqual(s.get.call_count, 1)

    def test_inline_stop_propagates_without_fallback(self):
        s = self.make_session()
        s.post.side_effect = [Response(data={'secret': SECRET, 'session_id': 'enroll-1'}), StopRequested('stop')]
        with self.assertRaises(StopRequested):
            export._try_inline_totp(s, 'token')
        self.assertEqual(s.post.call_count, 2)

    def test_non_object_mfa_info_is_not_false_disabled_state(self):
        s = self.make_session()
        s.get.return_value = Response(data=[])
        with self.assertRaisesRegex(RuntimeError, '非对象'):
            export._try_inline_totp(s, 'token')
        s.post.assert_not_called()


class RegistrationAuthenticationRecoveryTests(unittest.TestCase):
    def test_csrf_must_be_nonempty_string_in_success_object(self):
        for payload in ({}, [], {'csrfToken': ''}, {'csrfToken': '  '}, {'csrfToken': 12}):
            s = session()
            s.get.return_value = Response(data=payload)
            with self.assertRaisesRegex(RuntimeError, 'CSRF Token 获取失败'):
                chatgpt_auth.get_csrf_token(s)
        s.get.return_value = Response(data={'csrfToken': 'csrf-token'})
        self.assertEqual(chatgpt_auth.get_csrf_token(s), 'csrf-token')

    def test_registration_disallowed_removes_only_prompt(self):
        url = 'https://auth.openai.com/authorize?prompt=login&state=s&ccaps=a&ccaps=b&blank='
        with patch.object(chatgpt_auth, 'get_csrf_token', return_value='csrf'), \
             patch.object(chatgpt_auth, 'signin_openai', return_value=url), \
             patch.object(openai_auth, 'follow_authorize') as follow:
            s = session()
            recovered = flow.recover_registration_disallowed(s, 'a@example.com', RuntimeError('registration_disallowed'))
        follow.assert_not_called()
        s.get.assert_not_called()
        query = parse_qs(urlparse(recovered).query, keep_blank_values=True)
        self.assertNotIn('prompt', query)
        self.assertEqual(query, {'state': ['s'], 'ccaps': ['a', 'b'], 'blank': ['']})

    def test_mailbox_password_is_not_used_as_chatgpt_password(self):
        s = session()
        s.post.return_value = Response(data={'success': True})
        with patch('core.db.get_account_by_email', return_value={'password': 'outlook-password'}), \
             patch.object(openai_auth, 'request_sentinel_token', return_value={}), \
             patch.object(openai_auth, 'build_sentinel_header', return_value=('token', None)):
            password, data = flow.continue_existing_login(s, 'a@example.com', 'login_password',
                                                        {'continue_url': '/log-in/password'})
        self.assertIsNone(password)
        self.assertTrue(s.post.call_args.args[0].endswith('/passwordless/send-otp'))
        self.assertNotIn('data', s.post.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
