"""注册后置协议等待的停止响应；不访问外部服务。"""
import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from curl_cffi.requests import Cookies
from core import codex_oauth as codex, account_export as export
from core.registration_service import StopRequested


class ProtocolStopReviewTests(unittest.TestCase):
    def stopped(self):
        raise StopRequested('manual stop')

    def test_cancelled_retry_wrappers_do_not_send_request(self):
        for navigate in (False, True):
            with self.subTest(navigate=navigate):
                fn = MagicMock(return_value=SimpleNamespace(status_code=200))
                with patch('core.registration_service.check_stop_requested', side_effect=self.stopped):
                    with self.assertRaises(StopRequested):
                        if navigate:
                            codex._with_auth_navigation_retry(MagicMock(), 'offline', fn)
                        else:
                            codex._with_net_retry('offline', fn)
                fn.assert_not_called()

    def test_cancelled_workspace_does_not_return_cached_identity(self):
        cookies = Cookies()
        value = base64.urlsafe_b64encode(json.dumps(
            {'workspaces': [{'id': 'workspace'}]}).encode()).decode()
        cookies.set('oai-client-auth-session', value, domain='auth.openai.com')
        session = SimpleNamespace(session=SimpleNamespace(cookies=cookies))
        with patch('core.registration_service.check_stop_requested', side_effect=self.stopped):
            with self.assertRaises(StopRequested):
                codex._get_workspace_id(session)

    def test_network_backoff_is_interruptible(self):
        started = time.monotonic()
        def check():
            if time.monotonic() - started > .05:
                raise StopRequested('stop during backoff')
        fn = MagicMock(side_effect=RuntimeError('connection timed out'))
        with patch('core.registration_service.check_stop_requested', side_effect=check):
            with self.assertRaises(StopRequested):
                codex._with_net_retry('offline', fn)
        fn.assert_called_once()
        self.assertLess(time.monotonic() - started, .8)

    def test_phone_retry_delay_is_interruptible(self):
        with patch('core.registration_service.check_stop_requested', side_effect=self.stopped), \
             patch.object(codex.time, 'sleep') as sleep:
            with self.assertRaises(StopRequested):
                codex._sleep_before_phone_retry(1, 2)
        sleep.assert_not_called()

    def test_navigation_backoff_propagates_stop_without_retrying(self):
        fn = MagicMock(return_value=SimpleNamespace(status_code=503, text='unavailable'))
        session = MagicMock()
        with patch.object(codex, '_interruptible_sleep', side_effect=StopRequested('stop')):
            with self.assertRaises(StopRequested):
                codex._with_auth_navigation_retry(session, 'offline', fn)
        fn.assert_called_once()
        session.reset_circuit_breaker.assert_called_once()

    def test_workspace_does_not_swallow_stop_from_scope_read(self):
        session = SimpleNamespace(session=SimpleNamespace(cookies=Cookies()))
        with patch.object(codex, 'cookies_for_url', side_effect=StopRequested('stop')):
            with self.assertRaises(StopRequested):
                codex._get_workspace_id(session)

    def test_workspace_wait_propagates_stop(self):
        session = SimpleNamespace(session=SimpleNamespace(cookies=Cookies()))
        with patch.object(codex, '_interruptible_sleep', side_effect=StopRequested('stop')) as wait:
            with self.assertRaises(StopRequested):
                codex._get_workspace_id(session)
        wait.assert_called_once()

    def test_retry_limit_and_backoff_are_preserved(self):
        from unittest.mock import call
        error = RuntimeError('connection timed out')
        fn = MagicMock(side_effect=error)
        with patch.object(codex, '_interruptible_sleep') as wait:
            with self.assertRaises(RuntimeError) as caught:
                codex._with_net_retry('offline', fn)
        self.assertIs(caught.exception, error)
        self.assertEqual(fn.call_count, 3)
        self.assertEqual(wait.call_args_list, [call(1.0), call(2.0)])

    def test_cancelled_callback_uploads_do_not_send(self):
        for submit, transport in ((codex._submit_cpa_callback, '_cpa_request_json'),
                                  (codex._submit_sub2_callback, '_sub2_codex_request_json')):
            with self.subTest(submit=submit.__name__), \
                 patch.object(codex, transport) as request, \
                 patch('core.registration_service.check_stop_requested', side_effect=self.stopped):
                with self.assertRaises(StopRequested):
                    submit('https://localhost/callback?code=test&state=test', **(
                        {'session_id': 'sid'} if submit == codex._submit_sub2_callback else {}))
                request.assert_not_called()

    def test_callback_retry_classifier_never_retries_stop(self):
        self.assertFalse(codex._is_cpa_callback_retryable(
            StopRequested('timeout status=503 connection')))

    def test_cancelled_twofa_retry_wrappers_do_not_send(self):
        for wrapper, method, args in (
                (export._trigger_reauth_with_retry, '_trigger_reauth', ('a@example.test',)),
                (export._follow_reauth_with_retry, '_follow_reauth', ('auth-url',)),
                (export._validate_twofa_otp_with_retry, '_validate_reauth_otp', ('123456',))):
            with self.subTest(wrapper=wrapper.__name__), \
                 patch.object(export, method) as request, \
                 patch.object(export, '_warm_auth_document_for_reauth'), \
                 patch('core.registration_service.check_stop_requested', side_effect=self.stopped):
                with self.assertRaises(StopRequested):
                    wrapper(MagicMock(), *args)
                request.assert_not_called()

    def test_registration_dwell_can_stop_without_waiting(self):
        with patch('core.registration_service.check_stop_requested', side_effect=self.stopped), \
             patch.object(export.time, 'sleep') as sleep:
            with self.assertRaises(StopRequested):
                export.post_register_dwell('a@example.test', seconds_range='10,10')
        sleep.assert_not_called()
