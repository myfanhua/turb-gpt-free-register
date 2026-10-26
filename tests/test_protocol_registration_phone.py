"""注册 add-phone 仅使用假响应及模拟接码服务，不访问网络。"""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from core import protocol_phone as phone, protocol_registration as flow, account_export as export, sms_provider
from core.registration_service import StopRequested
from tests.test_protocol_registration_alignment import Response, session


def sending():
    return Response(data={'page': {'type': 'phone_otp_verification'}, 'continue_url': '/phone-verification'})


def finished():
    return Response(data={'page': {'type': 'external_url', 'payload': {'url': '/authorize/continue'}}})


class RegistrationPhoneTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(phone.settings, 'PROTOCOL_REGISTRATION_PHONE_ENABLED', True))
        stack.enter_context(patch.object(phone.sms_settings, 'SMS_MAX_RETRIES', 2))
        self.http = MagicMock()
        self.api = {}
        for name in ('_http', 'acquire_number', 'wait_for_sms_code', 'set_status', 'cancel', 'complete'):
            self.api[name] = stack.enter_context(patch.object(sms_provider, name))
        self.api['_http'].return_value = self.http
        self.api['acquire_number'].side_effect = [('a1', '12025550123'), ('a2', '12025550124')]
        self.api['wait_for_sms_code'].return_value = '123456'
        self.sleep = stack.enter_context(patch.object(phone.auth, '_interruptible_sleep'))
        self.s = session()
        self.s.post.side_effect = [sending(), finished()]

    def test_disabled_does_not_create_provider_session_or_take_number(self):
        with patch.object(phone.settings, 'PROTOCOL_REGISTRATION_PHONE_ENABLED', False):
            with self.assertRaisesRegex(RuntimeError, '协议配置'):
                phone.verify_registration_phone(self.s)
        self.api['_http'].assert_not_called()
        self.s.post.assert_not_called()

    def test_success_has_expected_body_and_completes_exactly_once(self):
        result = phone.verify_registration_phone(self.s)
        self.assertEqual(flow.step_info(result)[1], 'https://auth.openai.com/authorize/continue')
        send, validate = self.s.post.call_args_list
        self.assertEqual(json.loads(send.kwargs['data']), {'phone_number': '+12025550123'})
        self.assertNotIn('origin', send.kwargs['headers'])
        self.assertEqual(json.loads(validate.kwargs['data']), {'code': '123456'})
        self.api['complete'].assert_called_once_with('a1', self.http)
        self.api['cancel'].assert_not_called()
        self.http.close.assert_called_once()

    def test_phone_rejection_can_rotate_number_but_only_with_explicit_code(self):
        self.s.post.side_effect = [Response(400, {'error': {'code': 'phone_number_already_in_use'}}),
                                   sending(), finished()]
        phone.verify_registration_phone(self.s)
        self.assertEqual(self.api['acquire_number'].call_count, 2)
        self.api['cancel'].assert_called_once_with('a1', self.http)
        self.api['complete'].assert_called_once_with('a2', self.http)
        self.sleep.assert_called_once()

    def test_authentication_challenge_rate_limit_and_server_failure_do_not_rotate(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.s.post.side_effect = [Response(status, {'error': {'code': 'phone_number_already_in_use'}})]
                self.api['acquire_number'].reset_mock(side_effect=True)
                self.api['acquire_number'].return_value = ('a1', '12025550123')
                with self.assertRaises(Exception):
                    phone.verify_registration_phone(self.s)
                self.api['acquire_number'].assert_called_once()

    def test_wait_timeout_rotates_and_cancels_unused_number(self):
        self.api['wait_for_sms_code'].side_effect = [sms_provider.SmsCodeTimeout('timeout'), '123456']
        self.s.post.side_effect = [sending(), sending(), finished()]
        phone.verify_registration_phone(self.s)
        self.api['cancel'].assert_called_once_with('a1', self.http)
        self.api['complete'].assert_called_once_with('a2', self.http)

    def test_no_balance_does_not_retry(self):
        self.api['acquire_number'].side_effect = sms_provider.SmsNoBalanceError('no balance')
        with self.assertRaises(sms_provider.SmsNoBalanceError):
            phone.verify_registration_phone(self.s)
        self.api['acquire_number'].assert_called_once()
        self.api['cancel'].assert_not_called()
        self.http.close.assert_called_once()

    def test_no_inventory_has_bounded_retry_without_cancelling_unclaimed_number(self):
        self.api['acquire_number'].side_effect = sms_provider.SmsNoNumbersError('no numbers')
        with self.assertRaises(sms_provider.SmsNoNumbersError):
            phone.verify_registration_phone(self.s)
        self.assertEqual(self.api['acquire_number'].call_count, 2)
        self.api['cancel'].assert_not_called()
        self.sleep.assert_called_once()

    def test_stop_during_sms_wait_cancels_and_closes_without_rotation(self):
        self.api['wait_for_sms_code'].side_effect = StopRequested('connection stopped')
        with self.assertRaises(StopRequested):
            phone.verify_registration_phone(self.s)
        self.api['cancel'].assert_called_once_with('a1', self.http)
        self.api['acquire_number'].assert_called_once()
        self.http.close.assert_called_once()

    def test_unknown_post_outcome_is_not_replayed(self):
        self.s.post.side_effect = TimeoutError('request timed out')
        with self.assertRaises(TimeoutError):
            phone.verify_registration_phone(self.s)
        self.s.post.assert_called_once()
        self.api['acquire_number'].assert_called_once()
        self.api['cancel'].assert_called_once()

    def test_completion_bookkeeping_failure_never_revalidates(self):
        self.api['complete'].side_effect = RuntimeError('provider unavailable')
        self.assertTrue(phone.verify_registration_phone(self.s))
        self.api['acquire_number'].assert_called_once()
        self.api['cancel'].assert_not_called()

    def test_invalid_send_state_never_waits_for_sms(self):
        self.s.post.side_effect = [Response(data={'success': True})]
        with self.assertRaisesRegex(RuntimeError, '未返回手机验证码状态'):
            phone.verify_registration_phone(self.s)
        self.api['wait_for_sms_code'].assert_not_called()
        self.api['cancel'].assert_called_once()

    def test_invalid_validate_state_never_marks_completed(self):
        self.s.post.side_effect = [sending(), Response(data={'success': True})]
        with self.assertRaisesRegex(RuntimeError, '缺少可继续'):
            phone.verify_registration_phone(self.s)
        self.api['complete'].assert_not_called()
        self.api['cancel'].assert_called_once()

    def test_invalid_phone_number_or_code_is_not_submitted(self):
        self.api['acquire_number'].side_effect = [('a1', 'not-a-number')]
        with self.assertRaisesRegex(RuntimeError, '国际手机号'):
            phone.verify_registration_phone(self.s)
        self.s.post.assert_not_called()
        self.api['cancel'].assert_called_once()

    def test_invalid_sms_code_is_not_submitted(self):
        self.api['wait_for_sms_code'].return_value = 'html'
        with self.assertRaisesRegex(RuntimeError, '六位短信'):
            phone.verify_registration_phone(self.s)
        self.s.post.assert_called_once()
        self.api['cancel'].assert_called_once()

    def test_business_failure_is_not_success_even_for_http_200(self):
        self.s.post.side_effect = [sending(), Response(data={'success': False})]
        with self.assertRaisesRegex(RuntimeError, '业务失败'):
            phone.verify_registration_phone(self.s)
        self.api['complete'].assert_not_called()

    def test_direct_authenticated_continuation_skips_sms_wait(self):
        self.s.post.side_effect = [finished()]
        phone.verify_registration_phone(self.s)
        self.api['wait_for_sms_code'].assert_not_called()
        self.api['complete'].assert_called_once()
        self.s.post.assert_called_once()


class RegistrationPhoneIntegrationTests(unittest.TestCase):
    def test_redirect_chain_handles_phone_then_consumes_callback(self):
        s = session()
        s.get.side_effect = [Response(), Response(302, headers={'Location':
            'https://chatgpt.com/api/auth/callback/openai?code=c'}), Response()]
        with patch.object(phone, 'verify_registration_phone', return_value=finished().data) as verify:
            result = flow.follow_registration_callback(s, '/add-phone', 'https://auth.openai.com/')
        self.assertTrue(result.startswith('https://chatgpt.com/api/auth/callback/'))
        verify.assert_called_once_with(s)

    def test_local_h_status_does_not_call_grizzly_with_wrong_credentials(self):
        http = MagicMock()
        with patch.object(phone.sms_settings, 'SMS_PROVIDER', 'h'):
            self.assertEqual(sms_provider.set_status('h-id', 1, http), 'OK')
        http.get.assert_not_called()
        http.post.assert_not_called()

    def test_registration_phone_switch_is_exposed_in_both_ui_config(self):
        from webui.config_editor import EDITABLE_FIELDS
        field = next(f for f in EDITABLE_FIELDS if f['key'] == 'PROTOCOL_REGISTRATION_PHONE_ENABLED')
        self.assertEqual(field['group'], '协议配置')
        self.assertEqual(field['file'], 'register.py')
        self.assertEqual(field['type'], 'bool')

    def test_reauth_csrf_rejects_invalid_payload_before_signin(self):
        for payload in ({}, [], {'csrfToken': ''}, {'csrfToken': 42}):
            s = session()
            s.get.return_value = Response(data=payload)
            with self.assertRaisesRegex(RuntimeError, '有效 csrfToken'):
                export._trigger_reauth(s, 'a@example.com')
            s.post.assert_not_called()

    def test_reauth_warmup_does_not_swallow_stop(self):
        s = session()
        s.get.side_effect = StopRequested('stop')
        with self.assertRaises(StopRequested):
            export._warm_auth_document_for_reauth(s)
        s.get.assert_called_once()

    def test_stop_message_is_not_classified_as_transport_retry(self):
        self.assertFalse(export._is_retryable_reauth_error(StopRequested('connection timeout 403')))
