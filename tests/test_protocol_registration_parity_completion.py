"""协议差异补齐的离线契约与故障边界回归。"""
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import browser
from core import account_export, protocol_phone as phone, sms_provider
from core.registration_service import StopRequested
from core.session import BrowserSession
from tests.test_protocol_registration_alignment import Response, session
from webui import config_editor


class HeaderParityCompletionTests(unittest.TestCase):
    def make_session(self, family='chrome'):
        s = object.__new__(BrowserSession)
        s.browser_profile = {'send_client_hints': family == 'chrome', 'user_agent': 'test',
                             'accept_language': 'ja-JP', 'sec_ch_ua': 'test-brand'}
        s.datadog_origin = 'rum'
        s.datadog_trace_id, s.datadog_parent_id = '123', '456'
        return s

    def test_nextauth_has_matching_trace_headers_without_auth_invocation_headers(self):
        s = self.make_session()
        first, second = s.get_nextauth_headers(), s.get_nextauth_headers()
        self.assertEqual(first['accept'], 'application/json')
        self.assertEqual(first['accept-encoding'], 'gzip, deflate, br, zstd')
        self.assertEqual(first['tracestate'], 'dd=s:1;o:rum')
        self.assertIn(format(int(first['x-datadog-trace-id']), '032x'), first['traceparent'])
        self.assertNotEqual(first['traceparent'], second['traceparent'])
        self.assertNotIn('x-access-flow-invocation-id', first)
        self.assertNotIn('x-openai-document-navigation-id', first)
        self.assertNotIn('origin', first)
        self.assertNotIn('oai-device-id', first)

    def test_high_entropy_switch_is_hot_read_and_non_chromium_has_no_hints(self):
        s = self.make_session()
        with patch.object(browser, 'SEND_HIGH_ENTROPY_CLIENT_HINTS', True):
            h = s._get_common_headers()
            for key in ('full-version-list', 'platform-version', 'arch', 'bitness', 'model'):
                self.assertIn('sec-ch-ua-' + key, h)
            self.assertFalse(any(k.startswith('sec-ch-ua') for k in self.make_session('firefox')._get_common_headers()))
        with patch.object(browser, 'SEND_HIGH_ENTROPY_CLIENT_HINTS', False):
            self.assertNotIn('sec-ch-ua-full-version-list', s._get_common_headers())

    def test_new_settings_are_available_to_both_web_renderers(self):
        fields = {f['key']: f for f in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields['SEND_HIGH_ENTROPY_CLIENT_HINTS']['group'], '协议配置')
        for key in ('PROTOCOL_PHONE_RESEND_INTERVAL', 'PROTOCOL_PHONE_MAX_RESENDS', 'PROTOCOL_PHONE_CODE_ATTEMPTS'):
            self.assertEqual(fields[key]['file'], 'register.py')
            self.assertEqual(fields[key]['type'], 'int')
            self.assertEqual(fields[key]['group'], '协议配置')


class PhoneParityCompletionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(phone.settings, PROTOCOL_PHONE_RESEND_INTERVAL=20,
                                               PROTOCOL_PHONE_MAX_RESENDS=3, PROTOCOL_PHONE_CODE_ATTEMPTS=3))
        self.stack.enter_context(patch.object(phone.sms_settings, 'SMS_CODE_WAIT', 90))
        self.now = 0
        self.stack.enter_context(patch.object(phone.time, 'monotonic', side_effect=lambda: self.now))
        self.status = self.stack.enter_context(patch.object(sms_provider, 'set_status'))
        self.wait = self.stack.enter_context(patch.object(sms_provider, 'wait_for_sms_code'))
        self.s, self.http = session(), MagicMock()
        self.s.post.return_value = Response(data={'continue_url': '/authorize/continue'})

    def test_resend_has_no_body_and_is_bounded_for_same_number(self):
        def wait(*args, **kw):
            for now in (0, 19, 20, 21, 40, 60, 80):
                self.now = now
                kw['on_poll'](now)
            return '123456'
        self.wait.side_effect = wait
        phone._wait_and_validate(self.s, 'a1', self.http)
        resends = [c for c in self.s.post.call_args_list if c.args[0].endswith('/phone-otp/resend')]
        self.assertEqual(len(resends), 3)
        self.assertTrue(all('data' not in c.kwargs for c in resends))
        self.assertEqual(self.status.call_count, 3)
        self.assertEqual(json.loads(self.s.post.call_args.kwargs['data']), {'code': '123456'})

    def test_resend_can_be_disabled(self):
        def wait(*args, **kw):
            self.now = 40
            kw['on_poll'](40)
            return '123456'
        self.wait.side_effect = wait
        with patch.object(phone.settings, 'PROTOCOL_PHONE_MAX_RESENDS', 0):
            phone._wait_and_validate(self.s, 'a1', self.http)
        self.s.post.assert_called_once()
        self.status.assert_not_called()

    def test_delayed_poll_does_not_catch_up_with_burst_resends(self):
        def wait(*args, **kw):
            for now in (55, 56, 57, 75, 76):
                self.now = now
                kw['on_poll'](now)
            return '123456'
        self.wait.side_effect = wait
        phone._wait_and_validate(self.s, 'a1', self.http)
        self.assertEqual(self.status.call_count, 2)

    def test_wrong_code_reuses_number_and_excludes_submitted_code_without_resetting_deadline(self):
        self.s.post.side_effect = [Response(400, {'error': {'code': 'wrong_phone_otp_code'}}),
                                   Response(data={'continue_url': '/authorize/continue'})]
        seen = []
        def wait(*args, **kw):
            seen.append((set(kw['exclude_codes']), kw['max_wait']))
            self.now += 10
            return '111111' if len(seen) == 1 else '222222'
        self.wait.side_effect = wait
        phone._wait_and_validate(self.s, 'a1', self.http)
        self.assertEqual(seen, [(set(), 90), ({'111111'}, 80)])
        self.assertTrue(all(c.args[:2] == ('a1', self.http) for c in self.wait.call_args_list))
        self.status.assert_called_once_with('a1', 3, http=self.http)

    def test_resend_failure_and_stop_never_continue_polling_or_validation(self):
        for failure in (Response(403), Response(429), TimeoutError('network'), StopRequested('stop')):
            self.s.post.reset_mock(side_effect=True)
            self.s.post.side_effect = [failure]
            self.now = 0
            def wait(*args, **kw):
                self.now = 20
                kw['on_poll'](20)
                self.fail('failed resend must propagate')
            self.wait.side_effect = wait
            with self.assertRaises(Exception):
                phone._wait_and_validate(self.s, 'a1', self.http)
            self.s.post.assert_called_once()

    def test_blank_successful_resend_is_accepted_but_html_is_not(self):
        self.s.post.return_value = Response()
        self.s.post.return_value.json = MagicMock(side_effect=ValueError())
        self.assertEqual(phone._post(self.s, 'phone-otp/resend', None, '/phone-verification'), {})
        self.s.post.return_value.text = '<html>challenge</html>'
        with self.assertRaises(RuntimeError):
            phone._post(self.s, 'phone-otp/resend', None, '/phone-verification')


class SmsProviderPollHookTests(unittest.TestCase):
    def test_all_providers_filter_old_codes_and_keep_hook_optional(self):
        for provider in ('h', 'l', 'smsbower', 'grizzly'):
            with self.subTest(provider=provider), ExitStack() as stack:
                clock = SimpleNamespace(now=0)
                stack.enter_context(patch.object(sms_provider, '_provider', return_value=provider))
                stack.enter_context(patch.object(sms_provider.time, 'time', side_effect=lambda: clock.now))
                stack.enter_context(patch.object(sms_provider.time, 'sleep', side_effect=lambda seconds: setattr(clock, 'now', clock.now+seconds)))
                for name in ('_post_h_json', '_post_l_json'):
                    stack.enter_context(patch.object(sms_provider, name, side_effect=[{'code': '111111'}, {'code': '222222'}]))
                for name in ('_request_smsbower', '_request_grizzly'):
                    stack.enter_context(patch.object(sms_provider, name, side_effect=['STATUS_OK:111111', 'STATUS_OK:222222']))
                hook = MagicMock()
                code = sms_provider.wait_for_sms_code('a', MagicMock(), max_wait=10, poll_interval=1,
                                                     on_poll=hook, exclude_codes={'111111'})
                self.assertEqual(code, '222222')
                self.assertEqual(hook.call_count, 2)


class TwofaPostActivationParityTests(unittest.TestCase):
    def test_post_activation_reads_status_after_checkpoint_and_preserves_success_on_read_failure(self):
        for failure in (RuntimeError('network'), None):
            s = session()
            s.device_id = 'device'
            s.post.return_value = Response(data={'success': True})
            events = []
            def read(*args):
                events.append('read')
                if failure:
                    raise failure
                return {'mfa_enabled': True}
            with patch.object(account_export, '_checkpoint_totp', side_effect=lambda *a, **kw: events.append('save')), \
                 patch.object(account_export, '_read_totp_info', side_effect=read):
                self.assertTrue(account_export._activate_totp(s, 'token', 'JBSWY3DPEHPK3PXP', 'e1'))
            self.assertEqual(s._registration_totp_secret, 'JBSWY3DPEHPK3PXP')
            self.assertEqual(s.post.call_count, 1)
            self.assertEqual(events, ['save', 'read'])
