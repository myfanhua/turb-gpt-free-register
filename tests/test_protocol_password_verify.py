"""密码认证 flow、错误诊断和凭证保护的离线回归。"""
import json
import unittest
from unittest.mock import patch

from core import db, openai_auth as auth, protocol_registration as flow
from tests.test_protocol_registration_alignment import Response, session


class ProtocolPasswordVerifyTests(unittest.TestCase):
    def test_password_verify_uses_matching_sentinel_flow(self):
        s = session()
        s.post.return_value = Response(data={
            'page': {'type': 'email_otp_verification'}, 'continue_url': '/email-verification'})
        account = {'extra_json': json.dumps({'registration_password': 'saved-password'})}
        with patch.object(db, 'get_account_by_email', return_value=account), \
             patch.object(auth, 'request_sentinel_token', return_value={}) as request, \
             patch.object(auth, 'build_sentinel_header', return_value=('token', 'so')) as build:
            password, data = flow.continue_existing_login(s, 'test@example.com', 'login_password', {})
        request.assert_called_once_with(s, 'password_verify')
        build.assert_called_once_with(s, {}, 'password_verify')
        self.assertEqual(password, 'saved-password')
        self.assertTrue(flow.needs_email_otp(data))
        self.assertTrue(s.post.call_args.args[0].endswith('/password/verify'))
        headers = s.post.call_args.kwargs['headers']
        self.assertEqual(headers['openai-sentinel-token'], 'token')
        self.assertEqual(headers['openai-sentinel-so-token'], 'so')
        self.assertEqual(json.loads(s.post.call_args.kwargs['data']), {'password': 'saved-password'})

    def test_http_failure_has_safe_error_code_status_and_flow(self):
        for status, code in [(401, 'invalid_password'), (403, 'access_denied'), (429, 'rate_limit_exceeded')]:
            with self.subTest(status=status):
                s = session()
                response = Response(status, {'error': {'code': code, 'message': 'secret-password otp=123456'}})
                s.post.return_value = response
                with self.assertRaises(RuntimeError) as caught:
                    flow._post(s, '/api/accounts/password/verify', {'password': 'secret-password'}, '/log-in/password')
                self.assertIn(f'status={status}', str(caught.exception))
                self.assertIn(f'code={code}', str(caught.exception))
                self.assertNotIn('secret-password', str(caught.exception))
                self.assertNotIn('123456', str(caught.exception))
                self.assertIs(caught.exception.response, response)
                self.assertEqual(caught.exception.error_code, code)
                s.post.assert_called_once()  # 认证失败不能盲目重试或重置密码。

    def test_non_json_and_invalid_codes_are_unknown(self):
        for body in ('not-json', {'error': {'code': 'bad\nsecret=123456'}}, {}):
            s = session()
            s.post.return_value = Response(401, body)
            with self.assertRaisesRegex(RuntimeError, 'status=401, code=unknown'):
                flow._post(s, '/api/accounts/password/verify', None, '/log-in/password')

    def test_dead_account_remains_typed_exception(self):
        s = session()
        s.post.return_value = Response(401, {'error': {'code': 'account_deactivated'}})
        with self.assertRaises(auth.AccountUnusableError):
            flow._post(s, '/api/accounts/password/verify', None, '/log-in/password')

    def test_network_error_preserved_without_retries(self):
        s = session()
        timeout = TimeoutError('network timeout')
        s.post.side_effect = timeout
        with self.assertRaises(TimeoutError) as caught:
            flow._post(s, '/api/accounts/password/verify', None, '/log-in/password')
        self.assertIs(caught.exception, timeout)
        s.post.assert_called_once()
