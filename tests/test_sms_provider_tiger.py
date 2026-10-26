"""Tiger SMS 使用模拟 HTTP，不取真实号码、不产生费用。"""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from config import codex, env_loader
from core import sms_provider as sms
from webui import config_editor


class TigerSmsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in {
            'SMS_PROVIDER': 'tiger', 'TIGER_SMS_API_KEY': 'test-key',
            'TIGER_SMS_API_BASE': 'https://api.tiger-sms.com/stubs/handler_api.php',
            'SMS_SERVICE': 'dr', 'SMS_COUNTRY': '6', 'SMS_MAX_PRICE': '0.2',
        }.items():
            self.stack.enter_context(patch.object(codex, key, value))
        self.stack.enter_context(patch.dict(sms._ACQUIRED_AT, {}, clear=True))
        self.http = Mock()

    def responses(self, *texts, status=200):
        self.http.get.side_effect = [Mock(text=json.dumps(text) if isinstance(text, (dict, list)) else text, status_code=status) for text in texts]

    def test_acquire(self):
        self.responses({'activationId': '123', 'phoneNumber': '+18734721259', 'activationCost': 0.15})
        self.assertEqual(sms.acquire_number(self.http), ('123', '18734721259'))
        self.http.get.assert_called_once_with(codex.TIGER_SMS_API_BASE, params={
            'api_key': 'test-key', 'action': 'getNumberV2', 'service': 'dr',
            'country': '6', 'maxPrice': '0.2',
        })
        self.assertIn('123', sms._ACQUIRED_AT)
        self.http.close.assert_not_called()

    def test_purchase_price_limit_is_validated_before_network(self):
        for value in ('0', '-0.1', 'NaN', 'Infinity', '-Infinity', 'invalid', 0):
            with self.subTest(value=value), patch.object(codex, 'SMS_MAX_PRICE', value):
                with self.assertRaisesRegex(sms.SmsProviderError, '购买最高单价'):
                    sms.acquire_number(self.http)
        self.http.get.assert_not_called()

    def test_selected_bucket_price_is_sent_as_cap_without_fixed_price(self):
        self.responses({'activationId': '123', 'phoneNumber': '18734721259'})
        with patch.object(codex, 'SMS_MAX_PRICE', '0.1500'):
            sms.acquire_number(self.http)
        params = self.http.get.call_args.kwargs['params']
        self.assertEqual(params['action'], 'getNumberV2')
        self.assertEqual(params['maxPrice'], '0.1500')
        self.assertNotIn('fixedPrice', params)

    def test_blank_price_is_unlimited(self):
        self.responses({'activationId': '123', 'phoneNumber': '18734721259'})
        with patch.object(codex, 'SMS_MAX_PRICE', '  '):
            sms.acquire_number(self.http)
        self.assertNotIn('maxPrice', self.http.get.call_args.kwargs['params'])

    def test_price_limit_is_not_relaxed_when_no_numbers(self):
        self.responses({'title': 'NO_NUMBERS', 'details': 'No numbers available'})
        with patch.object(codex, 'SMS_MAX_PRICE', '0.15'):
            with self.assertRaises(sms.SmsNoNumbersError):
                sms.acquire_number(self.http)
        self.http.get.assert_called_once()
        self.assertEqual(self.http.get.call_args.kwargs['params']['maxPrice'], '0.15')

    def test_service_alias_and_overrides(self):
        self.responses({'activationId': '123', 'phoneNumber': '18734721259'})
        sms.acquire_number(self.http, service='ChatGPT', country='10')
        params = self.http.get.call_args.kwargs['params']
        self.assertEqual(params['service'], 'dr')
        self.assertEqual(params['country'], '10')

    def test_missing_credentials_do_not_request(self):
        for key in ('TIGER_SMS_API_KEY', 'TIGER_SMS_API_BASE'):
            with self.subTest(key=key), patch.object(codex, key, ''):
                with self.assertRaises(sms.SmsProviderError):
                    sms.acquire_number(self.http)
        self.http.get.assert_not_called()

    def test_acquire_errors_and_malformed(self):
        for text, error in [
            ('NO_BALANCE', sms.SmsNoBalanceError), ('NO_NUMBERS', sms.SmsNoNumbersError),
            ('NO_PROVIDERS', sms.SmsNoNumbersError), ('BAD_KEY', sms.SmsProviderError),
            ('BAD_COUNTRY', sms.SmsProviderError), ('BAD_MAX_PRICE', sms.SmsProviderError),
            ('ACCESS_NUMBER::123', sms.SmsProviderError),
            ('ACCESS_NUMBER:123:', sms.SmsProviderError),
            ('ACCESS_NUMBER:123:abc', sms.SmsProviderError),
            ('<html>error</html>', sms.SmsProviderError),
        ]:
            with self.subTest(text=text):
                self.responses(text)
                with self.assertRaises(error):
                    sms.acquire_number(self.http)
        self.assertFalse(sms._ACQUIRED_AT)

    def test_http_error_does_not_retry_purchase(self):
        self.responses('Too Many Requests', status=429)
        with self.assertRaisesRegex(sms.SmsProviderError, 'HTTP 429'):
            sms.acquire_number(self.http)
        self.http.get.assert_called_once()

    def test_poll_wait_states_and_excluded_code(self):
        self.responses({'verificationType': 0, 'sms': None},
                       {'verificationType': 1, 'sms': {'code': '111111'}},
                       {'verificationType': 1, 'sms': {'code': '222222'}})
        with patch('core.task_control.sleep'):
            self.assertEqual(sms.wait_for_sms_code('123', self.http, max_wait=5,
                             exclude_codes={'111111'}), '222222')
        self.assertEqual(self.http.get.call_count, 3)
        self.assertEqual(self.http.get.call_args.kwargs['params']['action'], 'getStatusV2')

    def test_poll_cancelled_and_unknown(self):
        for text in ({'title': 'NOT_FOUND'}, {}, {'verificationType': 7}, {'verificationType': 1, 'sms': None}, [], 'STATUS_OK:123456'):
            with self.subTest(text=text):
                self.responses(text)
                with self.assertRaises(sms.SmsProviderError):
                    sms.wait_for_sms_code('123', self.http, max_wait=1)

    def test_poll_timeout(self):
        with patch.object(sms.time, 'time', side_effect=[0, 2]):
            with self.assertRaises(sms.SmsCodeTimeout):
                sms.wait_for_sms_code('123', self.http, max_wait=1)
        self.http.get.assert_not_called()

    def test_set_status(self):
        for status in (1, 3, 6, 8, -1):
            self.responses({'status': 'success'})
            self.assertEqual(sms.set_status('123', status, self.http), 'OK')
            self.assertEqual(self.http.get.call_args.kwargs['params']['status'], str(status))
            self.assertEqual(self.http.get.call_args.kwargs['params']['action'], 'setStatusV2')

    def test_status_failures(self):
        for result in ({'title': 'EARLY_CANCEL_DENIED'}, {'title': 'SMS_RECEIVED'},
                       {'title': 'BAD_STATUS'}, {}, [], 'ACCESS_CANCEL'):
            self.responses(result)
            with self.assertRaises(sms.SmsProviderError):
                sms.set_status('123', 8, self.http)

    def test_complete(self):
        self.responses({'status': 'success'})
        sms._ACQUIRED_AT['123'] = 0
        sms.complete('123', self.http)
        self.assertNotIn('123', sms._ACQUIRED_AT)

    def test_cancel_waits_for_server_minimum(self):
        self.http.get.side_effect = [
            Mock(text=json.dumps({'title': 'EARLY_CANCEL_DENIED', 'info': {'minActivationTime': 180}}), status_code=409),
            Mock(text=json.dumps({'status': 'success'}), status_code=200),
        ]
        sms._ACQUIRED_AT['123'] = 100
        with patch.object(sms, '_http', return_value=self.http), \
             patch.object(sms.time, 'time', return_value=105), \
             patch.object(sms.time, 'sleep') as sleep:
            sms.cancel('123', background=False)
        sleep.assert_called_once_with(176)
        self.assertNotIn('123', sms._ACQUIRED_AT)
        self.http.close.assert_called_once()
        self.assertEqual(self.http.get.call_args.kwargs['params']['action'], 'setStatusV2')

    def test_cancel_success_does_not_sleep(self):
        self.responses({'status': 'success'})
        with patch.object(sms, '_http', return_value=self.http), patch.object(sms.time, 'sleep') as sleep, \
             self.assertLogs(sms.logger, level='INFO') as logs:
            sms.cancel('123', background=False)
        self.assertTrue(any('请求取消号码 id=123' in line for line in logs.output))
        self.assertTrue(any('号码取消成功 id=123' in line for line in logs.output))
        sleep.assert_not_called()

    def test_cancel_does_not_claim_success_when_sms_received(self):
        self.responses({'title': 'SMS_RECEIVED', 'info': {'code': '123456'}}, status=409)
        sms._ACQUIRED_AT['123'] = 0
        with patch.object(sms, '_http', return_value=self.http), patch.object(sms.time, 'sleep') as sleep:
            sms.cancel('123', background=False)
        self.assertIn('123', sms._ACQUIRED_AT)
        sleep.assert_not_called()

    def test_v2_error_envelopes_before_http_status(self):
        for status, error, expected in (
            (402, {'title': 'NO_BALANCE'}, sms.SmsNoBalanceError),
            (200, {'title': 'NO_NUMBERS'}, sms.SmsNoNumbersError),
            (200, {'title': 'NO_PROVIDERS'}, sms.SmsNoNumbersError),
            (400, {'title': 'WRONG_MAX_PRICE', 'info': {'min': 1}}, sms.SmsProviderError),
            (422, {'title': 'UNPROCESSABLE_ENTITY'}, sms.SmsProviderError),
            (403, {'title': 'BAD_ACTION'}, sms.SmsProviderError),
        ):
            with self.subTest(status=status, error=error):
                self.http.reset_mock()
                self.responses(error, status=status)
                with self.assertRaises(expected):
                    sms.acquire_number(self.http)
                self.http.get.assert_called_once()
                self.assertEqual(self.http.get.call_args.kwargs['params']['action'], 'getNumberV2')
                self.assertEqual(self.http.get.call_args.kwargs['params']['maxPrice'], '0.2')

    def test_no_v1_success_fallback(self):
        self.responses('ACCESS_NUMBER:123:18734721259')
        with self.assertRaises(sms.SmsProviderError):
            sms.acquire_number(self.http)
        self.http.get.assert_called_once()

    def test_background_cancel_captures_config(self):
        with patch.object(sms.threading, 'Thread') as thread:
            sms.cancel('123', self.http)
        self.assertEqual(thread.call_args.kwargs['args'],
                         ('123', codex.TIGER_SMS_API_BASE, 'test-key'))
        thread.return_value.start.assert_called_once()
        self.http.get.assert_not_called()

    def test_owned_session_closed_on_failure(self):
        self.responses('BAD_KEY')
        with patch.object(sms, '_http', return_value=self.http):
            with self.assertRaises(sms.SmsProviderError):
                sms.acquire_number()
        self.http.close.assert_called_once()

    def test_config_and_ui(self):
        self.assertIn('TIGER_SMS_API_KEY', env_loader.SECRET_ENV_KEYS)
        fields = {f['key']: f for f in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields['TIGER_SMS_API_KEY']['storage'], 'env')
        self.assertTrue(fields['TIGER_SMS_API_KEY']['secret'])
        self.assertIn('TIGER_SMS_API_BASE', fields)


if __name__ == '__main__':
    unittest.main()
