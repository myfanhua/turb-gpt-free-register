"""协议注册状态分支回归；全部使用假响应，不请求注册服务。"""
import base64
import importlib.util
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from requests.cookies import RequestsCookieJar

import main
from core import protocol_registration as flow, openai_auth, db, account_export
from core.session import BrowserSession


class Response:
    def __init__(self, status=200, data=None, headers=None, text=''):
        self.status_code = status
        self.data = {} if data is None else data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f'HTTP {self.status_code}')
            error.response = self
            raise error


def session():
    s = MagicMock()
    s.get_auth_headers.side_effect = lambda **kw: {'origin': 'https://auth.openai.com', **kw}
    s.get_auth_navigate_headers.side_effect = lambda **kw: dict(kw)
    s.get_chatgpt_navigate_headers.side_effect = lambda **kw: dict(kw)
    s.session.cookies = RequestsCookieJar()
    return s


class ProtocolBranchTests(unittest.TestCase):
    def test_external_url_nested_payload_and_top_level_precedence(self):
        data = {'page': {'type': 'external_url', 'payload': {'url': '/authorize/continue'}}}
        self.assertEqual(flow.step_info(data), ('external_url', 'https://auth.openai.com/authorize/continue'))
        data['continue_url'] = '/workspace'
        self.assertEqual(flow.step_info(data)[1], 'https://auth.openai.com/workspace')
        data.pop('continue_url')
        data['page']['payload']['url'] = 'https://example.com/callback'
        with self.assertRaisesRegex(RuntimeError, '跳转域名'):
            flow.step_info(data)

    def test_non_external_payload_is_not_a_navigation_url(self):
        self.assertEqual(flow.step_info({'page': {'type': 'about_you', 'payload': {'url': '/unrelated'}}}),
                         ('about_you', ''))
        self.assertEqual(flow.step_info({'page': {'type': 'external_url', 'payload': []}}),
                         ('external_url', ''))

    def test_additional_oauth_landings_do_not_probe_signup(self):
        s = session()
        for path in ('/choose-an-account', '/sign-in-with-chatgpt/consent'):
            self.assertEqual(flow.resolve_signup_step(s, 'a@example.com', path)[0], 'external_url')
        s.post.assert_not_called()

    def test_workspace_additional_ssr_formats_and_malformed_list(self):
        s = session()
        raw = base64.urlsafe_b64encode(json.dumps({'workspaces': 42}).encode()).decode()
        s.session.cookies.set('oai-client-auth-session', raw, domain='auth.openai.com')
        self.assertEqual(flow.workspace_id(s, '{"workspaceId":"w-camel"}'), 'w-camel')
        wid = '12345678-1234-1234-1234-123456789012'
        self.assertEqual(flow.workspace_id(s, '"workspaces",[9],{"id","'+wid+'"}'), wid)

    def test_workspace_query_fallback_only_on_workspace_path(self):
        s = session()
        s.post.return_value = Response(data={'page': {'type': 'external_url', 'payload': {'url': '/authorize/continue'}}})
        self.assertEqual(flow.select_registration_workspace(s, referer='https://auth.openai.com/workspace?workspace_id=w-query'),
                         'https://auth.openai.com/authorize/continue')
        self.assertEqual(json.loads(s.post.call_args.kwargs['data']), {'workspace_id': 'w-query'})
        s.post.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'workspace_id'):
            flow.select_registration_workspace(s, referer='https://auth.openai.com/consent?id=unrelated')
        s.post.assert_not_called()

    def test_callback_selects_unique_account_and_consumes_callback(self):
        s = session()
        sid = 'us_1234567890abcdefghijkl'
        s.get.side_effect = [Response(text=sid+' '+sid), Response(302, headers={'Location': '/'}), Response()]
        s.post.return_value = Response(data={'page': {'type': 'external_url', 'payload': {
            'url': 'https://chatgpt.com/api/auth/callback/openai?code=c&state=s'}}})
        self.assertEqual(flow.follow_registration_callback(s, '/choose-an-account', 'https://auth.openai.com/'),
                         'https://chatgpt.com/')
        self.assertTrue(s.post.call_args.args[0].endswith('/api/accounts/session/select'))
        self.assertEqual(json.loads(s.post.call_args.kwargs['data']), {'session_id': sid})

    def test_account_selection_rejects_ambiguity_and_missing_id(self):
        for html in ('', 'us_1234567890abcdefghijkl us_abcdefghijkl1234567890'):
            s = session()
            with self.assertRaisesRegex(RuntimeError, '唯一 session_id'):
                flow.select_registration_account(s, referer='https://auth.openai.com/choose-an-account', html=html)
            s.post.assert_not_called()

    def test_cookie_only_account_selection_does_not_loop(self):
        s = session()
        s.get.return_value = Response(text='us_1234567890abcdefghijkl')
        s.post.return_value = Response(data={'success': True})
        with self.assertRaisesRegex(RuntimeError, '账号选择后仍返回'):
            flow.follow_registration_callback(s, '/choose-an-account', 'https://auth.openai.com/')
        self.assertEqual(s.get.call_count, 2)
        self.assertEqual(s.post.call_count, 1)

    def test_callback_rejects_empty_success_status(self):
        s = session()
        s.get.return_value = Response(204)
        with self.assertRaisesRegex(RuntimeError, '非预期状态'):
            flow.follow_registration_callback(s, 'https://chatgpt.com/api/auth/callback/openai', 'https://auth.openai.com/')

    def test_known_landing_does_not_probe_authorize_continue(self):
        s = session()
        for path, expected in [('/email-verification', 'new'), ('/create-account/password', 'new'),
                               ('/log-in/password', 'login_password'), ('/about-you', 'about_you'),
                               ('/log-in', 'login')]:
            self.assertEqual(flow.resolve_signup_step(s, 'a@example.com', 'https://auth.openai.com'+path)[0], expected)
        s.post.assert_not_called()

    def test_nested_passwordless_modes_are_classified_like_reference(self):
        for mode, kind in (('passwordless_signup', 'new'), ('passwordless_login', 'existing_otp')):
            s = session()
            s.post.return_value = Response(data={'page': {'type': 'email_otp_verification',
                'payload': {'email_verification_mode': mode}}, 'continue_url': '/email-verification'})
            with patch.object(openai_auth, 'request_sentinel_token', return_value={}), \
                 patch.object(openai_auth, 'build_sentinel_header', return_value=('token', None)):
                actual, data = flow.resolve_signup_step(s, 'a@example.com', '/authorize')
            self.assertEqual(actual, kind)
            if mode == 'passwordless_login':
                self.assertTrue(data['_otp_already_sent'])

    def test_login_transition_preserves_nested_auto_sent_mode(self):
        s = session()
        s.post.return_value = Response(data={'page': {'type': 'email_otp_verification',
            'payload': {'email_verification_mode': 'passwordless_login'}}})
        with patch.object(db, 'get_account_by_email', return_value=None), \
             patch.object(openai_auth, 'request_sentinel_token', return_value={}), \
             patch.object(openai_auth, 'build_sentinel_header', return_value=('token', None)):
            _, data = flow.continue_existing_login(s, 'a@example.com', 'login', {})
        self.assertTrue(data['_otp_already_sent'])
        self.assertEqual(s.post.call_count, 1)

    def test_email_verification_query_does_not_trigger_otp(self):
        self.assertFalse(flow.needs_email_otp({'continue_url': '/authorize/continue?next=/email-verification'}))
        self.assertFalse(flow.needs_email_otp({'continue_url': '/email-verification-extra'}))
        self.assertTrue(flow.needs_email_otp({'continue_url': '/email-verification/'}))

    def test_register_create_and_resend_preserve_dead_account_error(self):
        s = session()
        s.post.return_value = Response(403, {'error': {'code': 'account_deactivated'}})
        calls = (
            lambda: openai_auth.register_user(s, 'a@example.com', 'p', 't'),
            lambda: openai_auth.create_account(s, 'Test', '1990-01-01', 't'),
            lambda: flow.resend_registration_otp(s),
        )
        for call in calls:
            with self.assertRaises(openai_auth.AccountUnusableError):
                call()

    def test_nested_workspace_query_is_not_a_workspace_page(self):
        s = session()
        s.get.return_value = Response(text='{}')
        with patch.object(flow, 'select_registration_workspace') as select:
            with self.assertRaisesRegex(RuntimeError, '停留在认证页面'):
                flow.follow_registration_callback(s, '/not-a-workspace?next=/workspace', 'https://auth.openai.com/')
        select.assert_not_called()

    def test_unknown_landing_probes_with_signup_parameters(self):
        s = session()
        s.post.return_value = Response(data={'page': {'type': 'passwordless_login'},
                                            'continue_url': '/email-verification'})
        with patch.object(openai_auth, 'request_sentinel_token', return_value={}) as challenge, \
             patch.object(openai_auth, 'build_sentinel_header', return_value=('token', 'so')):
            kind, data = flow.resolve_signup_step(s, 'a@example.com', 'https://auth.openai.com/authorize')
        self.assertEqual(kind, 'existing_otp')
        self.assertTrue(flow.needs_email_otp(data))
        args, kwargs = s.post.call_args
        self.assertTrue(args[0].endswith('/api/accounts/authorize/continue'))
        self.assertEqual(json.loads(kwargs['data']), {
            'username': {'value': 'a@example.com', 'kind': 'email'}, 'screen_hint': 'signup'})
        self.assertNotIn('origin', kwargs['headers'])
        self.assertEqual(kwargs['headers']['openai-sentinel-so-token'], 'so')
        challenge.assert_called_once_with(s, 'authorize_continue')

    def test_resend_uses_current_challenge_without_send(self):
        s = session()
        s.post.return_value = Response()
        with patch.object(openai_auth, 'send_email_otp') as send:
            flow.resend_registration_otp(s, existing=True)
        self.assertTrue(s.post.call_args.args[0].endswith('/email-otp/resend'))
        send.assert_not_called()

    def test_new_account_resend_has_ordered_fallback(self):
        s = session()
        s.post.side_effect = [Response(409), Response(404)]
        with patch.object(openai_auth, 'send_email_otp') as send:
            flow.resend_registration_otp(s)
        self.assertEqual([c.args[0].split('/api/accounts/')[-1] for c in s.post.call_args_list],
                         ['email-otp/resend', 'passwordless/send-otp'])
        send.assert_called_once_with(s)

    def test_http_200_business_failure_is_not_successful_otp_delivery(self):
        s = session()
        s.post.return_value = Response(data={'success': False})
        with self.assertRaisesRegex(RuntimeError, '业务失败'):
            flow.resend_registration_otp(s)
        with patch.object(db, 'get_account_by_email', return_value=None), \
             patch.object(openai_auth, 'request_sentinel_token', return_value={}), \
             patch.object(openai_auth, 'build_sentinel_header', return_value=('token', None)):
            with self.assertRaisesRegex(RuntimeError, '业务失败'):
                flow.continue_existing_login(s, 'a@example.com', 'login_password',
                                             {'continue_url': '/log-in/password'})

    def test_existing_account_does_not_try_signup_passwordless_fallback(self):
        s = session()
        s.post.return_value = Response(409)
        with patch.object(openai_auth, 'send_email_otp') as send:
            flow.resend_registration_otp(s, existing=True)
        self.assertEqual(s.post.call_count, 1)
        send.assert_called_once_with(s)

    def test_resend_does_not_hide_network_or_rate_limit_errors(self):
        for status in (401, 403, 429, 500):
            s = session()
            s.post.return_value = Response(status)
            with patch.object(openai_auth, 'send_email_otp') as send:
                with self.assertRaises(RuntimeError):
                    flow.resend_registration_otp(s)
                send.assert_not_called()
                self.assertEqual(s.post.call_count, 1)

    def test_existing_login_uses_saved_password_and_mfa(self):
        s = session()
        s.post.side_effect = [
            Response(data={'page': {'type': 'mfa_challenge'}, 'continue_url': '/mfa-challenge/challenge-1'}),
            Response(data={'page': {'type': 'external_url'}, 'continue_url': '/authorize/continue?state=x'}),
        ]
        account = {'extra_json': json.dumps({'registration_password': 'saved-password'}),
                   'totp_secret': 'JBSWY3DPEHPK3PXP'}
        with patch.object(db, 'get_account_by_email', return_value=account), \
             patch.object(openai_auth, 'request_sentinel_token', return_value={}), \
             patch.object(openai_auth, 'build_sentinel_header', return_value=('token', 'so')):
            password, data = flow.continue_existing_login(
                s, 'a@example.com', 'login_password', {'continue_url': '/log-in/password'})
        self.assertEqual(password, 'saved-password')
        self.assertFalse(flow.needs_email_otp(data))
        self.assertEqual(json.loads(s.post.call_args_list[0].kwargs['data']), {'password': 'saved-password'})
        body = json.loads(s.post.call_args_list[1].kwargs['data'])
        self.assertEqual(body['id'], 'challenge-1')
        self.assertEqual(body['type'], 'totp')
        self.assertEqual(len(body['code']), 6)

    def test_existing_login_never_guesses_password(self):
        s = session()
        s.post.return_value = Response(data={'success': True})
        with patch.object(db, 'get_account_by_email', return_value=None), \
             patch.object(openai_auth, 'request_sentinel_token', return_value={}), \
             patch.object(openai_auth, 'build_sentinel_header', return_value=('token', None)):
            password, data = flow.continue_existing_login(
                s, 'a@example.com', 'login_password', {'continue_url': '/log-in/password'})
        self.assertIsNone(password)
        self.assertTrue(flow.needs_email_otp(data))
        self.assertTrue(s.post.call_args.args[0].endswith('/passwordless/send-otp'))
        self.assertNotIn('data', s.post.call_args.kwargs)

    def test_mfa_without_secret_fails_before_sending_code(self):
        s = session()
        with patch.object(db, 'get_account_by_email', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'TOTP'):
                flow.continue_existing_login(s, 'a@example.com', 'mfa_challenge',
                                             {'continue_url': '/mfa-challenge/challenge-1'})
        s.post.assert_not_called()

    def test_workspace_decode_skips_jwt_header_and_uses_payload(self):
        s = session()
        enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
        raw = enc({'alg': 'none'}) + '.' + enc({'workspaces': [{'id': 'workspace-1'}]}) + '.sig'
        s.session.cookies.set('oai-client-auth-session', raw, domain='auth.openai.com')
        self.assertEqual(flow.workspace_id(s), 'workspace-1')
        s.post.return_value = Response(data={'continue_url': '/authorize/continue'})
        self.assertEqual(flow.select_registration_workspace(s, referer='https://auth.openai.com/workspace'),
                         'https://auth.openai.com/authorize/continue')
        self.assertEqual(json.loads(s.post.call_args.kwargs['data']), {'workspace_id': 'workspace-1'})

    def test_workspace_ssr_escaped_html_and_malformed_cookie(self):
        s = session()
        s.session.cookies.set('oai-client-auth-session', 'invalid.payload', domain='auth.openai.com')
        self.assertEqual(flow.workspace_id(s, r'{\"workspace_id\":\"workspace-2\"}'), 'workspace-2')
        self.assertEqual(flow.workspace_id(s, '{"id":"unrelated"}'), '')

    def test_callback_selects_workspace_then_consumes_chatgpt_callback(self):
        s = session()
        s.get.side_effect = [Response(text='{"workspace_id":"w1"}'),
                             Response(302, headers={'Location': 'https://chatgpt.com/api/auth/callback/openai?code=c&state=s'}),
                             Response(302, headers={'Location': '/'}), Response()]
        s.post.return_value = Response(data={'continue_url': '/authorize/continue'})
        result = flow.follow_registration_callback(s, 'https://auth.openai.com/workspace', 'https://auth.openai.com/about-you')
        self.assertEqual(result, 'https://chatgpt.com/')
        urls = [c.args[0] for c in s.get.call_args_list]
        self.assertIn('https://chatgpt.com/api/auth/callback/openai?code=c&state=s', urls)
        self.assertTrue(all(c.kwargs['allow_redirects'] is False for c in s.get.call_args_list))
        self.assertTrue(all(c.kwargs['user_initiated'] is False for c in s.get_auth_navigate_headers.call_args_list))

    def test_callback_rejects_offsite_redirect_before_requesting_it(self):
        s = session()
        s.get.return_value = Response(302, headers={'Location': 'https://example.com/steal'})
        with self.assertRaisesRegex(RuntimeError, '跳转域名'):
            flow.follow_registration_callback(s, 'https://auth.openai.com/authorize/continue', 'https://auth.openai.com/about-you')
        self.assertEqual(s.get.call_count, 1)

    def test_callback_bounds_redirect_loop(self):
        s = session()
        s.get.return_value = Response(302, headers={'Location': '/loop'})
        with self.assertRaisesRegex(RuntimeError, '次数'):
            flow.follow_registration_callback(s, 'https://auth.openai.com/loop', 'https://auth.openai.com/about-you')
        self.assertEqual(s.get.call_count, 16)

    def test_unfinished_auth_document_is_not_treated_as_callback_success(self):
        s = session()
        s.get.return_value = Response()
        with self.assertRaisesRegex(RuntimeError, '停留在认证页面'):
            flow.follow_registration_callback(s, '/email-verification', 'https://auth.openai.com/about-you')

    def test_registration_disallowed_only_recovers_once(self):
        s = session()
        error = RuntimeError('registration_disallowed')
        with patch('core.chatgpt_auth.get_csrf_token', return_value='csrf'), \
             patch('core.chatgpt_auth.signin_openai', return_value='https://auth.openai.com/authorize'), \
             patch.object(openai_auth, 'follow_authorize') as follow:
            self.assertEqual(flow.recover_registration_disallowed(s, 'a@example.com', error),
                             'https://auth.openai.com/authorize')
            follow.assert_not_called()
        with patch('core.chatgpt_auth.get_csrf_token') as csrf:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                flow.recover_registration_disallowed(s, 'a@example.com', RuntimeError('HTTP 403'))
            csrf.assert_not_called()


class DeviceIdentityTests(unittest.TestCase):
    def test_constructor_failure_closes_partial_session_and_relay(self):
        raw, relay = MagicMock(), MagicMock()
        def fail(s, *_args, **_kw):
            s.session = raw
            s._proxy_pool_relay = relay
            raise ValueError('invalid profile')
        with patch.object(BrowserSession, '_initialize', fail):
            with self.assertRaisesRegex(ValueError, 'invalid profile'):
                BrowserSession()
        raw.close.assert_called_once()
        relay.close.assert_called_once()

    def test_constructor_cleanup_failure_preserves_original_error(self):
        raw, relay = MagicMock(), MagicMock()
        raw.close.side_effect = RuntimeError('close failed')
        def fail(s, *_args, **_kw):
            s.session, s._proxy_pool_relay = raw, relay
            raise ValueError('initialization failed')
        with patch.object(BrowserSession, '_initialize', fail):
            with self.assertRaisesRegex(ValueError, 'initialization failed'):
                BrowserSession()
        relay.close.assert_called_once()

    def make_session(self, cookies):
        s = object.__new__(BrowserSession)
        s.session = SimpleNamespace(cookies=cookies)
        s.device_id = 'initial'
        s.sentinel_sid = 'sdk-sid'
        s._sentinel_context_p = {'top_level': 'old-proof'}
        cookies.set('oai-did', 'initial', domain='chatgpt.com')
        cookies.set('oai-did', 'initial', domain='auth.openai.com')
        cookies.set('__cf_bm', 'cf-value', domain='.chatgpt.com')
        return s

    def check_sync(self, cookies):
        s = self.make_session(cookies)
        cookies.set('oai-did', 'server-device', domain='.chatgpt.com')
        self.assertEqual(s.sync_device_id_from_cookies(), 'server-device')
        self.assertEqual(s.device_id, 'server-device')
        self.assertTrue(all(c.value == 'server-device' for c in cookies.jar if c.name == 'oai-did'))
        self.assertEqual(cookies.get('__cf_bm'), 'cf-value')
        self.assertEqual(s.sentinel_sid, 'sdk-sid')
        self.assertEqual(s._sentinel_context_p, {})
        # 幂等同步不再丢弃新生成的 proof。
        s._sentinel_context_p = {'top_level': 'new-proof'}
        s.sync_device_id_from_cookies()
        self.assertEqual(s._sentinel_context_p, {'top_level': 'new-proof'})

    def test_curl_cookie_jar(self):
        from curl_cffi.requests.cookies import Cookies
        self.check_sync(Cookies())

    @unittest.skipUnless(importlib.util.find_spec('wreq'), 'wreq 未安装')
    def test_wreq_native_jar_not_only_enumeration_snapshot(self):
        import wreq
        from core.wreq_session import WreqCookies
        self.check_sync(WreqCookies(wreq.Jar(), wreq))

    def test_parent_domain_cookie_is_applied_to_auth_identity(self):
        from curl_cffi.requests.cookies import Cookies
        cookies = Cookies()
        s = self.make_session(cookies)
        cookies.set('oai-did', 'server-auth', domain='.openai.com')
        self.assertEqual(s.sync_device_id_from_cookies('auth.openai.com'), 'server-auth')
        self.assertTrue(all(c.value == 'server-auth' for c in cookies.jar if c.name == 'oai-did'))


class RegistrationChainTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patches = {}
        for name in ('BrowserSession', 'human_delay', 'network_preflight', 'get_providers', 'get_csrf_token',
                     'signin_openai', 'follow_authorize', 'navigate_create_account_password',
                     'generate_registration_password', 'request_password_sentinel_bundle', 'register_user',
                     'navigate_email_otp_send', 'request_sentinel_token', 'build_sentinel_header',
                     'validate_email_otp', 'navigate_about_you', 'create_account',
                     '_finalize_registration_session', 'save_account_data', 'wait_for_otp',
                     'resend_registration_otp', 'continue_existing_login', 'install_registration_checkpoint', 'checkpoint_registration_password', 'reauthorize_registration_session', 'fetch_session'):
            self.patches[name] = stack.enter_context(patch.object(main, name))
        p = self.patches
        s = p['BrowserSession'].return_value
        s.proxy = ''
        s.device_id = s.oai_session_id = s.auth_session_logging_id = 'device-123456789'
        s.fingerprint_summary.return_value = {}
        p['follow_authorize'].return_value = 'https://auth.openai.com/email-verification'
        p['build_sentinel_header'].return_value = ('token', 'so')
        p['register_user'].return_value = {'continue_url': '/api/accounts/email-otp/send'}
        p['validate_email_otp'].return_value = {'page': {'type': 'about_you'}, 'continue_url': '/about-you'}
        p['create_account'].return_value = {'continue_url': '/authorize/continue'}
        p['_finalize_registration_session'].return_value = ({'user': {}, 'account': {}}, 'access-token')
        p['save_account_data'].return_value = 1
        p['fetch_session'].return_value = {}
        stack.enter_context(patch.object(main._roxy_cfg, 'REGISTRATION_DRIVER', 'protocol'))
        stack.enter_context(patch.object(main._email_cfg, 'USE_EMAIL_SERVICE', True))
        stack.enter_context(patch.object(main._twofa_cfg, 'ENABLE_2FA', False))
        stack.enter_context(patch.multiple(main._protocol_cfg, CHATGPT_ANON_BOOTSTRAP_ENABLED=False,
                                         CHATGPT_AUTH_BOOTSTRAP_ENABLED=False,
                                         SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE=True))
        stack.enter_context(patch('core.codex_oauth.run_codex_oauth', return_value={'status': 'skipped'}))
        stack.enter_context(patch('core.flow_trigger.trigger_flow', return_value={'status': 'skipped'}))
        self.release = stack.enter_context(patch('core.email_provider.release_email'))
        stack.enter_context(patch('core.email_provider.resolve_email_source', return_value='test'))

    def run_registration(self, otp='123456'):
        return main.run_registration('a@example.com', 'Test User', '1990-01-01', otp_code=otp)

    def test_final_session_refresh_updates_saved_token_and_checkpoint(self):
        p = self.patches
        latest = {'accessToken': 'final-token', 'user': {'email': 'a@example.com'}}
        p['fetch_session'].return_value = latest
        self.assertTrue(self.run_registration()['success'])
        self.assertEqual(p['save_account_data'].call_args.kwargs['access_token'], 'final-token')
        self.assertEqual(p['BrowserSession'].return_value._latest_authenticated_session, latest)
        p['fetch_session'].assert_called_once()

    def test_final_session_read_failure_keeps_already_confirmed_credentials(self):
        p = self.patches
        p['fetch_session'].side_effect = TimeoutError('network')
        self.assertTrue(self.run_registration()['success'])
        self.assertEqual(p['save_account_data'].call_args.kwargs['access_token'], 'access-token')

    def test_final_session_identity_mismatch_is_not_ignored(self):
        p = self.patches
        p['fetch_session'].return_value = {'accessToken': 'wrong-token', 'user': {'email': 'other@example.com'}}
        self.assertFalse(self.run_registration()['success'])
        p['save_account_data'].assert_not_called()

    def test_final_session_stop_propagates(self):
        from core.registration_service import StopRequested
        self.patches['fetch_session'].side_effect = StopRequested('stop')
        with self.assertRaises(StopRequested):
            self.run_registration()
        self.patches['save_account_data'].assert_not_called()

    def test_codex_runs_after_password_and_secret_checkpoints(self):
        p = self.patches
        events = []
        p['checkpoint_registration_password'].side_effect = lambda *_: events.append('password')
        p['install_registration_checkpoint'].return_value.side_effect = lambda **kw: events.append(('session', kw.get('totp_secret')))
        with patch.object(main._twofa_cfg, 'ENABLE_2FA', True), \
             patch.object(main, 'setup_2fa', return_value='confirmed-secret'), \
             patch('core.codex_oauth.run_codex_oauth', side_effect=lambda *_: events.append('codex') or {'status': 'skipped'}):
            self.assertTrue(self.run_registration()['success'])
        self.assertEqual(events, ['password', ('session', None), ('session', 'confirmed-secret'), 'codex'])
        self.assertFalse(p['save_account_data'].call_args.kwargs['auto_twofa'])
        self.assertEqual(p['save_account_data'].call_args.kwargs['extra']['registration_stage'], 'complete')

    def test_existing_passwordless_otp_does_not_resend(self):
        p = self.patches
        for data in (
            {'page': {'type': 'passwordless_login'}, 'continue_url': '/email-verification'},
            {'page': {'type': 'email_otp_verification'}, 'continue_url': '/email-verification', '_otp_already_sent': True},
        ):
            p['resend_registration_otp'].reset_mock()
            p['continue_existing_login'].return_value = (None, data)
            with patch.object(main, 'resolve_signup_step', return_value=('existing_otp', data)):
                self.assertTrue(self.run_registration()['success'])
            p['resend_registration_otp'].assert_not_called()

    def test_web_manual_otp_uses_job_channel_not_terminal(self):
        from core import registration_service
        with patch.object(main._email_cfg, 'USE_EMAIL_SERVICE', False), \
             patch.object(registration_service._THREAD_CTX, 'job_id', 123, create=True), \
             patch.object(registration_service, 'is_stop_requested', return_value=False), \
             patch('builtins.input', side_effect=AssertionError('terminal blocked')):
            self.patches['wait_for_otp'].return_value = '123456'
            self.assertTrue(self.run_registration(otp=None)['success'])
        self.patches['wait_for_otp'].assert_called_once()

    def test_existing_empty_otp_state_reauthorizes_once(self):
        p = self.patches
        p['continue_existing_login'].return_value = (None, {'page': {'type': 'passwordless_login'}})
        p['validate_email_otp'].return_value = {}
        p['reauthorize_registration_session'].return_value = 'https://auth.openai.com/authorize/continue'
        with patch.object(main, 'resolve_signup_step', return_value=('existing_otp', {})):
            self.assertTrue(self.run_registration()['success'])
        p['reauthorize_registration_session'].assert_called_once()
        p['create_account'].assert_not_called()

    def test_stop_after_saved_password_keeps_recoverable_email(self):
        from core.registration_service import StopRequested
        self.patches['validate_email_otp'].side_effect = StopRequested('stop')
        with self.assertRaises(StopRequested):
            self.run_registration()
        self.patches['checkpoint_registration_password'].assert_called_once()
        self.release.assert_not_called()

    def test_initial_add_phone_uses_challenge_not_password_or_email_otp(self):
        p = self.patches
        with patch.object(main, 'resolve_signup_step', return_value=('add_phone', {'continue_url': '/add-phone'})), \
             patch('core.protocol_phone.verify_registration_phone', return_value={'continue_url': '/authorize/continue'}) as phone:
            self.assertTrue(self.run_registration()['success'])
        phone.assert_called_once_with(p['BrowserSession'].return_value)
        p['register_user'].assert_not_called()
        p['validate_email_otp'].assert_not_called()
        p['create_account'].assert_not_called()

    def test_email_otp_mfa_then_phone_returns_to_about_you(self):
        p = self.patches
        p['validate_email_otp'].return_value = {'page': {'type': 'mfa_challenge'}, 'continue_url': '/mfa-challenge/id'}
        p['continue_existing_login'].return_value = ('saved', {'page': {'type': 'add_phone'}, 'continue_url': '/add-phone'})
        with patch('core.protocol_phone.verify_registration_phone', return_value={'continue_url': '/about-you'}) as phone:
            self.assertTrue(self.run_registration()['success'])
        phone.assert_called_once()
        p['create_account'].assert_called_once()

    def test_repeated_phone_challenge_does_not_take_another_number(self):
        self.patches['validate_email_otp'].return_value = {'page': {'type': 'add_phone'}, 'continue_url': '/add-phone'}
        with patch('core.protocol_phone.verify_registration_phone', return_value={'continue_url': '/add-phone'}) as phone:
            self.assertFalse(self.run_registration()['success'])
        phone.assert_called_once()

    def test_new_registration_sequence_uses_email_otp_validate_flow(self):
        result = self.run_registration()
        self.assertTrue(result['success'])
        flows = [c.args[1] for c in self.patches['request_sentinel_token'].call_args_list]
        self.assertEqual(flows, ['email_otp_validate', 'oauth_create_account'])
        self.patches['register_user'].assert_called_once()
        self.patches['resend_registration_otp'].assert_not_called()
        self.release.assert_not_called()

    def test_nested_otp_external_url_skips_create_account(self):
        p = self.patches
        p['validate_email_otp'].return_value = {'page': {'type': 'external_url', 'payload': {'url': '/authorize/continue'}}}
        self.assertTrue(self.run_registration()['success'])
        p['create_account'].assert_not_called()
        self.assertEqual(p['_finalize_registration_session'].call_args.args[1], 'https://auth.openai.com/authorize/continue')

    def test_initial_nested_external_url_survives_branch_normalization(self):
        p = self.patches
        with patch.object(main, 'resolve_signup_step', return_value=('external_url', {
            'page': {'type': 'external_url', 'payload': {'url': '/authorize/continue'}}})):
            self.assertTrue(self.run_registration()['success'])
        p['register_user'].assert_not_called()
        p['validate_email_otp'].assert_not_called()
        self.assertEqual(p['_finalize_registration_session'].call_args.args[1], 'https://auth.openai.com/authorize/continue')

    def test_create_account_nested_external_url_does_not_select_workspace(self):
        p = self.patches
        p['create_account'].return_value = {'page': {'type': 'external_url', 'payload': {'url': '/authorize/continue'}}}
        with patch.object(main, 'select_registration_workspace') as select:
            self.assertTrue(self.run_registration()['success'])
        select.assert_not_called()
        self.assertEqual(p['_finalize_registration_session'].call_args.args[1], 'https://auth.openai.com/authorize/continue')

    def test_empty_otp_response_does_not_create_account(self):
        p = self.patches
        p['validate_email_otp'].return_value = {}
        self.assertFalse(self.run_registration()['success'])
        p['create_account'].assert_not_called()

    def test_session_constructor_failure_releases_acquired_email(self):
        p = self.patches
        p['BrowserSession'].side_effect = ValueError('bad backend config')
        with patch.object(main, 'acquire_email', return_value='a@example.com'), \
             patch.object(main, 'close_browser_session') as close:
            result = main.run_registration(None, 'Test User', '1990-01-01')
        self.assertFalse(result['success'])
        self.assertEqual(self.release.call_args.kwargs['status'], 'available')
        close.assert_called_once_with(None)

    def test_acquired_email_callback_failure_releases_email(self):
        with patch.object(main, 'acquire_email', return_value='a@example.com'):
            result = main.run_registration(None, 'Test User', '1990-01-01',
                                           on_email_acquired=MagicMock(side_effect=RuntimeError('db unavailable')))
        self.assertFalse(result['success'])
        self.release.assert_called_once()
        self.patches['BrowserSession'].assert_not_called()

    def test_otp_followed_by_mfa_uses_existing_credentials(self):
        p = self.patches
        p['validate_email_otp'].return_value = {'page': {'type': 'mfa_challenge'},
                                               'continue_url': '/mfa-challenge/challenge-2'}
        p['continue_existing_login'].return_value = (None, {'continue_url': '/authorize/continue'})
        self.assertTrue(self.run_registration()['success'])
        self.assertEqual(p['continue_existing_login'].call_args.args[2], 'mfa_challenge')
        p['create_account'].assert_not_called()

    def test_manual_stop_in_twofa_is_not_swallowed_as_optional_failure(self):
        from core.registration_service import StopRequested
        with patch.object(main._twofa_cfg, 'ENABLE_2FA', True), \
             patch.object(main, 'setup_2fa', side_effect=StopRequested('stop')), \
             patch.object(main, 'close_browser_session') as close, \
             patch('core.codex_oauth.run_codex_oauth') as codex:
            with self.assertRaises(StopRequested):
                self.run_registration()
        codex.assert_not_called()
        self.patches['save_account_data'].assert_not_called()
        close.assert_called_once()

    def test_registration_twofa_prefers_inline_and_saves_refreshed_session(self):
        p = self.patches
        s = p['BrowserSession'].return_value
        refreshed = {'accessToken': 'refreshed-token', 'user': {'email': 'a@example.com'}, 'account': {}}
        def setup(*_args, **_kwargs):
            s._latest_authenticated_session = refreshed
            return 'secret'
        with patch.object(main._twofa_cfg, 'ENABLE_2FA', True), patch.object(main, 'setup_2fa', side_effect=setup) as twofa:
            result = self.run_registration()
        self.assertTrue(result['success'])
        twofa.assert_called_once_with(s, 'a@example.com', access_token='access-token', prefer_inline=True)
        self.assertEqual(p['save_account_data'].call_args.kwargs['access_token'], 'refreshed-token')

    def test_unconfirmed_twofa_preserves_pending_material_but_not_active_secret(self):
        p = self.patches
        s = p['BrowserSession'].return_value
        pending = {'secret': 'pending-secret', 'session_id': 'enroll-id', 'factor_id': 'factor-id'}
        def setup(*_args, **_kwargs):
            s._pending_totp_enrollment = pending
            raise RuntimeError('activation not confirmed')
        with patch.object(main._twofa_cfg, 'ENABLE_2FA', True), patch.object(main, 'setup_2fa', side_effect=setup):
            self.assertTrue(self.run_registration()['success'])
        saved = p['save_account_data'].call_args.kwargs
        self.assertIsNone(saved['totp_secret'])
        self.assertEqual(saved['extra']['pending_totp_enrollment'], pending)

    def test_refreshed_identity_mismatch_does_not_save_account(self):
        p = self.patches
        p['BrowserSession'].return_value._latest_authenticated_session = {
            'accessToken': 'other-token', 'user': {'email': 'other@example.com'}}
        self.assertFalse(self.run_registration()['success'])
        p['save_account_data'].assert_not_called()

    def test_manual_stop_in_codex_does_not_save_task_as_normal_failure(self):
        from core.registration_service import StopRequested
        with patch('core.codex_oauth.run_codex_oauth', side_effect=StopRequested('stop')), \
             patch.object(main, 'close_browser_session') as close:
            with self.assertRaises(StopRequested):
                self.run_registration()
        self.patches['save_account_data'].assert_not_called()
        close.assert_called_once()

    def test_unclassified_otp_url_does_not_create_account(self):
        p = self.patches
        p['validate_email_otp'].return_value = {'continue_url': '/add-phone'}
        self.assertFalse(self.run_registration()['success'])
        p['create_account'].assert_not_called()

    def test_invalid_otp_resends_then_uses_new_token(self):
        p = self.patches
        p['validate_email_otp'].side_effect = [openai_auth.EmailOtpInvalidError('expired'),
                                             {'page': {'type': 'external_url'}, 'continue_url': '/authorize/continue'}]
        p['wait_for_otp'].return_value = '654321'
        result = self.run_registration()
        self.assertTrue(result['success'])
        p['resend_registration_otp'].assert_called_once_with(p['BrowserSession'].return_value, existing=False)
        self.assertEqual([c.args[1] for c in p['validate_email_otp'].call_args_list], ['123456', '654321'])
        self.assertEqual([c.args[1] for c in p['request_sentinel_token'].call_args_list],
                         ['email_otp_validate', 'email_otp_validate'])
        p['create_account'].assert_not_called()

    def test_otp_timeout_has_bounded_resend(self):
        p = self.patches
        p['wait_for_otp'].side_effect = TimeoutError('no mail')
        result = self.run_registration(otp=None)
        self.assertFalse(result['success'])
        self.assertEqual(p['wait_for_otp'].call_count, 3)
        self.assertEqual(p['resend_registration_otp'].call_count, 2)
        p['validate_email_otp'].assert_not_called()

    def test_existing_login_skips_register_password(self):
        p = self.patches
        p['follow_authorize'].return_value = 'https://auth.openai.com/log-in/password'
        p['continue_existing_login'].return_value = ('saved', {'page': {'type': 'email_otp_verification'},
                                                            'continue_url': '/email-verification'})
        p['validate_email_otp'].return_value = {'page': {'type': 'external_url'}, 'continue_url': '/authorize/continue'}
        self.assertTrue(self.run_registration()['success'])
        p['register_user'].assert_not_called()
        p['request_password_sentinel_bundle'].assert_not_called()
        p['resend_registration_otp'].assert_called_once_with(p['BrowserSession'].return_value, existing=True)
        self.assertEqual(p['save_account_data'].call_args.kwargs['extra']['registration_password'], 'saved')

    def test_authorize_about_you_skips_otp_and_password(self):
        p = self.patches
        p['follow_authorize'].return_value = 'https://auth.openai.com/about-you'
        self.assertTrue(self.run_registration(otp=None)['success'])
        p['register_user'].assert_not_called()
        p['validate_email_otp'].assert_not_called()
        p['wait_for_otp'].assert_not_called()
        p['create_account'].assert_called_once()

    def test_unknown_otp_state_does_not_blindly_create_account(self):
        self.patches['validate_email_otp'].return_value = {'page': {'type': 'add_phone'}}
        self.assertFalse(self.run_registration()['success'])
        self.patches['create_account'].assert_not_called()

    def test_registration_closes_session_on_success_and_failure(self):
        p = self.patches
        with patch.object(main, 'close_browser_session') as close:
            self.assertTrue(self.run_registration()['success'])
            close.assert_called_once_with(p['BrowserSession'].return_value)
        p['validate_email_otp'].side_effect = RuntimeError('HTTP 403')
        with patch.object(main, 'close_browser_session') as close:
            self.assertFalse(self.run_registration()['success'])
            close.assert_called_once_with(p['BrowserSession'].return_value)

    def test_missing_continue_url_uses_workspace_fallback(self):
        p = self.patches
        p['create_account'].return_value = {}
        with patch.object(main, 'select_registration_workspace', return_value='https://auth.openai.com/authorize/continue') as select:
            self.assertTrue(self.run_registration()['success'])
            select.assert_called_once()


class FinalizeSessionTests(unittest.TestCase):
    def test_callback_network_timeout_does_not_replay_possibly_consumed_code(self):
        s = session()
        s.get.side_effect = [Response(302, headers={'Location': 'https://chatgpt.com/api/auth/callback/openai?code=c'}),
                             TimeoutError('connection timed out')]
        with patch.object(main, 'follow_registration_callback', wraps=flow.follow_registration_callback) as callback, \
             patch.object(main, 'human_delay'), \
             patch.object(main, 'fetch_session', return_value={'accessToken': 'token'}), \
             patch.object(openai_auth, '_interruptible_sleep'):
            _, token = main._finalize_registration_session(s, '/authorize/continue', 'a@example.com')
        self.assertEqual(token, 'token')
        callback.assert_called_once()
        self.assertEqual(s.get.call_count, 2)

    def test_session_identity_mismatch_is_not_retried(self):
        with patch.object(main, 'follow_registration_callback') as callback, \
             patch.object(main, 'human_delay'), \
             patch.object(main, 'fetch_session', return_value={'accessToken': 'token', 'user': {'email': 'other@example.com'}}), \
             patch.object(openai_auth, '_interruptible_sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '邮箱与注册目标不匹配'):
                main._finalize_registration_session(session(), '/authorize/continue', 'a@example.com')
        callback.assert_called_once()
        sleep.assert_not_called()

    def test_session_retry_does_not_consume_callback_code_twice(self):
        s = session()
        with patch.object(main, 'follow_registration_callback') as callback, \
             patch.object(main, 'human_delay'), \
             patch.object(main, 'fetch_session', side_effect=[RuntimeError('connection reset'), {'accessToken': 'token'}]), \
             patch.object(openai_auth, '_interruptible_sleep') as sleep:
            _, token = main._finalize_registration_session(s, '/authorize/continue', 'a@example.com')
        self.assertEqual(token, 'token')
        callback.assert_called_once()
        sleep.assert_called_once_with(1.0)

    def test_nonretryable_callback_state_fails_without_replaying(self):
        with patch.object(main, 'follow_registration_callback', side_effect=RuntimeError('unsupported state')) as callback, \
             patch.object(openai_auth, '_interruptible_sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'unsupported state'):
                main._finalize_registration_session(session(), '/authorize/continue', 'a@example.com')
        callback.assert_called_once()
        sleep.assert_not_called()

    def test_manual_stop_is_not_retried_as_callback_failure(self):
        from core.registration_service import StopRequested
        s = session()
        with patch.object(main, 'follow_registration_callback', side_effect=StopRequested('stop')) as callback, \
             patch.object(openai_auth, '_interruptible_sleep') as sleep:
            with self.assertRaises(StopRequested):
                main._finalize_registration_session(s, '/authorize/continue', 'a@example.com')
        callback.assert_called_once()
        sleep.assert_not_called()


class AuthResponseValidationTests(unittest.TestCase):
    def test_three_main_endpoints_reject_http_200_business_error(self):
        calls = [lambda s: openai_auth.register_user(s, 'a@example.com', 'Password!1', 'sentinel'),
                 lambda s: openai_auth.validate_email_otp(s, '123456'),
                 lambda s: openai_auth.create_account(s, 'User', '1990-01-01', 'sentinel')]
        for call in calls:
            for data in ({'success': False}, {'error': {'code': 'invalid_auth_step'}}):
                s = session()
                s.post.return_value = Response(data=data)
                with self.subTest(call=call, data=data), self.assertRaisesRegex(RuntimeError, '业务失败'):
                    call(s)

    def test_auth_response_rejects_unexpected_status_and_structure(self):
        for response in (Response(204), Response(302), Response(data=[])):
            with self.assertRaises(RuntimeError):
                openai_auth.parse_auth_step_response(response, 'test')
        response = Response()
        response.json = MagicMock(side_effect=ValueError('html body'))
        with self.assertRaisesRegex(RuntimeError, '非 JSON'):
            openai_auth.parse_auth_step_response(response, 'test')

    def test_http_200_registration_disallowed_keeps_response_for_recovery(self):
        response = Response(data={'error': {'code': 'registration_disallowed'}})
        with self.assertRaisesRegex(RuntimeError, 'registration_disallowed') as caught:
            openai_auth.parse_auth_step_response(response, 'create_account')
        self.assertIs(caught.exception.response, response)

    def test_http_200_dead_account_has_nonretryable_type(self):
        with self.assertRaises(openai_auth.AccountUnusableError):
            openai_auth.parse_auth_step_response(Response(data={'error': {'code': 'account_deactivated'}}), 'test')

    def test_session_access_token_alias_and_malformed_optional_fields(self):
        s = session()
        s.get.return_value = Response(data={'access_token': 'token', 'user': [], 'account': 'invalid'})
        data = account_export.fetch_session(s)
        self.assertEqual(data['accessToken'], 'token')
        self.assertEqual(data['user'], {})
        self.assertEqual(data['account'], {})

    def test_session_rejects_non_object_invalid_token_and_business_error(self):
        for data in ([], {'accessToken': ['token']}, {'accessToken': 'token', 'success': False}):
            s = session()
            s.get.return_value = Response(data=data)
            with self.assertRaises(RuntimeError):
                account_export.fetch_session(s)


if __name__ == '__main__':
    unittest.main()
