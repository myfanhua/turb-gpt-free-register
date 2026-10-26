"""收尾审计：真实临时 SQLite + 假认证服务，禁止调用线上注册。"""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import db, protocol_registration as flow, account_export as export, openai_auth as auth, codex_oauth as codex
from core.registration_service import StopRequested
from tests.test_protocol_registration_alignment import Response, session


class RegistrationCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        names = ('_ACCOUNTS_JSON', '_OUTLOOK_JSON', '_GENERIC_API_EMAIL_JSON', '_DOMAIN_EMAIL_JSON',
                 '_JOBS_JSON', '_LEGACY_ACCOUNTS_JSON', '_LEGACY_OUTLOOK_JSON', '_LEGACY_JOBS_JSON',
                 '_LEGACY_SQLITE', '_LEGACY_CODEX_EXPORT_STATE', '_CODEX_DIR', '_CODEX_AGENT_DIR',
                 '_TOKENS_TXT', '_ACCOUNTS_TXT')
        paths = {name: root / name for name in names}
        paths.update(_SQLITE_PATH=root / 'audit.sqlite3', _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        p = patch.multiple(db, **paths)
        p.start()
        self.addCleanup(p.stop)
        db._ensure_sqlite()

    def put(self, email='a@example.test', token='token', **kwargs):
        return db.checkpoint_registration_account(email=email, access_token=token, extra=kwargs.pop('extra', {}), **kwargs)

    def test_password_then_pending_then_confirmed_is_readable_by_codex(self):
        rid = self.put(token='', extra={'registration_stage': 'password_registered', 'registration_password': 'saved-password'})
        self.assertEqual(codex._account_registration_password('a@example.test'), 'saved-password')
        self.assertFalse(db.get_account_by_email('a@example.test')['access_token'])
        pending = {'secret': 'pending', 'session_id': 'e', 'factor_id': 'f'}
        self.assertEqual(self.put(extra={'pending_totp_enrollment': pending}), rid)
        self.assertEqual(codex._account_totp_secret('a@example.test'), '')
        self.put(totp_secret='confirmed', extra={'pending_totp_enrollment': None})
        self.assertEqual(codex._account_totp_secret('a@example.test'), 'confirmed')
        self.assertEqual(codex._account_registration_password('a@example.test'), 'saved-password')

    def test_partial_password_checkpoint_never_erases_existing_token_or_metadata(self):
        rid = self.put(totp_secret='secret', extra={'email_service': {'order': 'o'}, 'other': 1})
        self.put(token='', extra={'registration_stage': 'password_registered', 'registration_password': 'password'})
        row = db.get_account_by_email('a@example.test')
        self.assertEqual(row['id'], rid)
        self.assertEqual(row['access_token'], 'token')
        self.assertEqual(row['totp_secret'], 'secret')
        self.assertEqual(json.loads(row['extra_json'])['email_service'], {'order': 'o'})

    def test_final_save_preserves_checkpoint_metadata(self):
        self.put(extra={'registration_password': 'password', 'email_service': {'order': 'o'}, 'historical': True})
        db.insert_account(email='a@example.test', access_token='final', extra={'registration_stage': 'complete', 'pending_totp_enrollment': None})
        extra = json.loads(db.get_account_by_email('a@example.test')['extra_json'])
        self.assertEqual(extra['registration_password'], 'password')
        self.assertEqual(extra['email_service'], {'order': 'o'})
        self.assertTrue(extra['historical'])
        self.assertEqual(extra['registration_stage'], 'complete')

    def test_checkpoint_never_loads_or_rewrites_the_account_collection(self):
        with patch.object(db, '_load_accounts', side_effect=AssertionError('full scan')), \
             patch.object(db, '_save_accounts', side_effect=AssertionError('full rewrite')):
            self.put()
            self.put(token='new', extra={'other': 'value'})

    def test_concurrent_same_email_has_one_id_and_retains_all_fields(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda i: self.put(extra={f'field_{i}': i}), range(8)))
        self.assertEqual(len(set(ids)), 1)
        extra = json.loads(db.get_account_by_email('a@example.test')['extra_json'])
        self.assertTrue(all(extra[f'field_{i}'] == i for i in range(8)))

    def test_deleted_account_ids_are_not_reused_by_checkpoints(self):
        first = self.put()
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute('DELETE FROM accounts')
        self.assertGreater(self.put(email='b@example.test'), first)

    def test_empty_token_is_only_allowed_for_confirmed_password_registration(self):
        for extra in ({}, {'registration_stage': 'password_registered'}, {'registration_password': 'p'}):
            with self.assertRaises(ValueError):
                self.put(token='', extra=extra)

    def test_checkpoint_callback_is_deduplicated_and_restores_saved_credentials(self):
        pending = {'secret': 'pending-secret', 'session_id': 'e', 'factor_id': 'f'}
        self.put(extra={'registration_password': 'saved', 'pending_totp_enrollment': pending})
        s = SimpleNamespace(device_id='d', browser_profile={}, sentinel_sid='s', proxy='')
        with patch('core.email_provider.resolve_email_source', return_value='imap'), \
             patch.object(db, 'checkpoint_registration_account', wraps=db.checkpoint_registration_account) as write:
            checkpoint = flow.install_registration_checkpoint(s, 'a@example.test', {'accessToken': 'token'}, 'token', None)
            self.assertEqual(s._registration_password, 'saved')
            self.assertEqual(s._pending_totp_enrollment, pending)
            checkpoint(pending=pending)
            checkpoint(pending=pending)
            self.assertEqual(write.call_count, 1)
            checkpoint(totp_secret='confirmed')
        self.assertEqual(codex._account_totp_secret('a@example.test'), 'confirmed')

    def test_manual_twofa_resumes_saved_registration_enrollment(self):
        from core.twofa_service import _restore_registration_enrollment
        pending = {'secret': 'JBSWY3DPEHPK3PXP', 'session_id': 'e', 'factor_id': 'f'}
        rid = self.put(extra={'pending_totp_enrollment': pending, 'registration_password': 'saved'})
        s = session()
        s.device_id, s.browser_profile, s.sentinel_sid, s.proxy = 'd', {}, 's', ''
        with patch('core.email_provider.resolve_email_source', return_value='imap'):
            self.assertTrue(_restore_registration_enrollment(s, rid, 'a@example.test', 'token'))
        self.assertEqual(s._pending_totp_enrollment, pending)
        s.get.return_value = Response(data={'mfa_enabled': True, 'factors': {'totp': {'id': 'f'}}})
        self.assertEqual(export._try_inline_totp(s, 'token'), pending['secret'])
        s.post.assert_not_called()
        self.assertIsNone(json.loads(db.get_account(rid)['extra_json'])['pending_totp_enrollment'])

    def test_checkpoint_preserves_activated_secret_if_process_stops_after_activation(self):
        s = session()
        s.device_id, s.browser_profile, s.sentinel_sid, s.proxy = 'd', {}, 's', ''
        with patch('core.email_provider.resolve_email_source', return_value='imap'):
            checkpoint = flow.install_registration_checkpoint(s, 'a@example.test', {'accessToken': 'token'}, 'token', 'password')
            checkpoint()
            s._pending_totp_enrollment = {'secret': 'pending', 'session_id': 'e'}
            export._checkpoint_totp(s, pending=s._pending_totp_enrollment)
            s.post.return_value = Response(data={'success': True})
            export._activate_totp(s, 'token', 'JBSWY3DPEHPK3PXP', 'e')
        self.assertEqual(codex._account_totp_secret('a@example.test'), 'JBSWY3DPEHPK3PXP')
        self.assertIsNone(json.loads(db.get_account_by_email('a@example.test')['extra_json'])['pending_totp_enrollment'])


class RegistrationContractTests(unittest.TestCase):
    def test_malformed_url_values_and_credentials_are_rejected(self):
        for value in ({'url': '/about-you'}, ['/about-you'], ' ', 'https://auth.openai.com:bad/a',
                      'https://auth.openai.com:444/a', 'https://user@auth.openai.com/a',
                      'https://auth.openai.com.evil/a', 'https://auth.openai.com/a\nheader', '\\evil/a'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                flow.normalize_auth_url(value)

    def test_route_classification_uses_path_not_query_or_arbitrary_substrings(self):
        for url in ('/log-in-extra', '/foo/about-you', '/not-add-phone?next=/about-you',
                    'https://chatgpt.com/log-in/password'):
            self.assertEqual(flow.landing_step(url), 'unknown')
        self.assertEqual(flow.landing_step('https://chatgpt.com/'), 'external_url')

    def test_workspace_selection_is_not_replayed_after_later_network_failure(self):
        s = session()
        s.get.side_effect = [Response(text='{"workspace_id":"w"}'), TimeoutError('network'),
                             Response(302, headers={'Location': 'https://chatgpt.com/'}), Response()]
        s.post.return_value = Response(data={'continue_url': '/authorize/continue'})
        with self.assertRaises(TimeoutError):
            flow.follow_registration_callback(s, '/workspace', 'https://auth.openai.com/about-you')
        self.assertEqual(flow.follow_registration_callback(s, '/workspace', 'https://auth.openai.com/about-you'),
                         'https://chatgpt.com/')
        s.post.assert_called_once()

    def test_resend_does_not_accept_html_or_array_as_delivery_success(self):
        for data in ([], 'not an object'):
            s = session()
            s.post.return_value = Response(data=data)
            with self.assertRaisesRegex(RuntimeError, '非对象'):
                flow.resend_registration_otp(s)
        s.post.return_value = Response(text='<html>challenge</html>')
        s.post.return_value.json = MagicMock(side_effect=ValueError('not json'))
        with self.assertRaisesRegex(RuntimeError, '非 JSON'):
            flow.resend_registration_otp(s)

    def test_business_error_has_same_dead_account_type_in_login_branch(self):
        s = session()
        s.post.return_value = Response(401, data={'error': {'code': 'account_deactivated'}})
        with self.assertRaises(auth.AccountUnusableError):
            flow._post(s, '/api/accounts/password/verify', {}, 'https://auth.openai.com/log-in/password')

    def test_expired_otp_409_is_retryable_but_invalid_state_is_not_wrong_code(self):
        s = session()
        s.post.return_value = Response(409, data={'error': {'code': 'wrong_email_otp_code'}})
        with self.assertRaises(auth.EmailOtpInvalidError):
            auth.validate_email_otp(s, '123456')
        s.post.return_value = Response(400, data={'error': {'code': 'invalid_state'}}, text='invalid_state code')
        with self.assertRaises(RuntimeError) as caught:
            auth.validate_email_otp(s, '123456')
        self.assertNotIsInstance(caught.exception, auth.EmailOtpInvalidError)

    def test_about_you_redirect_to_login_cannot_create_account(self):
        s = session()
        s.get.return_value = Response()
        s.get.return_value.url = 'https://auth.openai.com/log-in'
        with self.assertRaisesRegex(RuntimeError, '未进入资料页'):
            auth.navigate_about_you(s)

    def test_auth_navigation_rejects_offsite_before_sending_request(self):
        for fn in (auth.navigate_email_otp_send, auth.navigate_about_you):
            s = session()
            with self.assertRaisesRegex(RuntimeError, '跳转域名'):
                fn(s, 'https://other.example/about-you')
            s.get.assert_not_called()

    def test_pending_totp_is_resumed_without_new_enrollment(self):
        s = session()
        s._pending_totp_enrollment = {'secret': 'JBSWY3DPEHPK3PXP', 'session_id': 'e', 'factor_id': 'f'}
        s.get.return_value = Response(data={'mfa_enabled': False})
        s.post.return_value = Response(data={'success': True})
        with patch.object(export, '_enroll_totp') as enroll:
            secret = export._try_inline_totp(s, 'token')
        self.assertEqual(secret, 'JBSWY3DPEHPK3PXP')
        enroll.assert_not_called()
        self.assertTrue(s.post.call_args.args[0].endswith('/activate_enrollment'))

    def test_pending_activated_factor_is_recovered_by_exact_id_without_post(self):
        s = session()
        s._pending_totp_enrollment = {'secret': 'saved-secret', 'session_id': 'e', 'factor_id': 'f'}
        s.get.return_value = Response(data={'mfa_enabled': True, 'factors': {'totp': {'id': 'f'}}})
        self.assertEqual(export._try_inline_totp(s, 'token'), 'saved-secret')
        s.post.assert_not_called()

    def test_pending_material_checkpoint_failure_prevents_activation(self):
        s = session()
        s._registration_credential_checkpoint = MagicMock(side_effect=OSError('disk full'))
        s.get.return_value = Response(data={'mfa_enabled': False})
        s.post.return_value = Response(data={'secret': 'secret', 'session_id': 'e'})
        with self.assertRaisesRegex(OSError, 'disk full'):
            export._try_inline_totp(s, 'token')
        self.assertEqual(s.post.call_count, 1)

    def test_codex_otp_uses_matching_flow_and_closes_session_on_stop(self):
        s = session()
        s.post.return_value = Response(data={'continue_url': '/authorize/continue'})
        with patch.object(codex, 'request_sentinel_token', return_value={}) as token, \
             patch.object(codex, 'build_sentinel_header', return_value=('token', None)):
            codex._submit_email_otp(s, '123456')
        token.assert_called_once_with(s, 'email_otp_validate')
        with patch.object(codex._cfg, 'CODEX_OAUTH_DRIVER', 'protocol'), \
             patch.object(codex, 'BrowserSession', return_value=s), \
             patch.object(codex, '_codex_auth_url_source', side_effect=StopRequested('stop')), \
             patch.object(codex, 'close_browser_session') as close:
            with self.assertRaises(StopRequested):
                codex.run_codex_oauth('a@example.test', force=True)
        close.assert_called_once_with(s)

    def test_save_pending_enrollment_never_queues_another_twofa_task(self):
        with patch.object(db, 'insert_account', return_value=1), \
             patch.object(export, '_append_batch_archive'), patch('config.twofa.ENABLE_2FA', True), \
             patch('core.twofa_service.enqueue_account_totp_setup') as enqueue:
            export.save_account_data('a@example.test', 'token', extra={'pending_totp_enrollment': {'session_id': 'e'}},
                                     auto_plan_check=False)
            export.save_account_data('b@example.test', 'token', auto_twofa=False, auto_plan_check=False)
        enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
