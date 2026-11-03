import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db, registration_service as svc
from webui.app import _compact_account_for_list, create_app


class RegistrationResumeTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        missing = root / 'missing.json'
        stack.enter_context(patch.multiple(
            db, _ACCOUNTS_JSON=root/'accounts.json', _JOBS_JSON=root/'jobs.json',
            _OUTLOOK_JSON=missing, _GENERIC_API_EMAIL_JSON=missing, _DOMAIN_EMAIL_JSON=missing,
            _LEGACY_ACCOUNTS_JSON=missing, _LEGACY_JOBS_JSON=missing, _LEGACY_OUTLOOK_JSON=missing,
            _LEGACY_SQLITE=root/'missing.db', _CODEX_DIR=root/'codex', _CODEX_AGENT_DIR=root/'agent',
            _LEGACY_CODEX_EXPORT_STATE=missing, _LOG_DIR=root/'logs',
            _SQLITE_READY=False, _SQLITE_READY_PATH=None))
        self.account = {'id': 3624, 'email': 'original@example.com', 'email_source': 'generic_api',
                        'access_token': '', 'extra_json': json.dumps({
                            'registration_stage': 'password_registered', 'registration_password': 'saved-password'})}
        db._save_accounts([self.account])

    def test_resume_job_deduplicates_and_retains_original_account(self):
        executor = MagicMock()
        with patch.object(svc, 'get_executor', return_value=executor):
            first = svc.submit_registration_resume(3624)
            second = svc.submit_registration_resume(3624)
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(first['job']['id'], second['job']['id'])
        self.assertEqual(first['job']['email'], self.account['email'])
        self.assertEqual(first['job']['email_source'], 'generic_api')
        executor.submit.assert_called_once()
        db.update_job(first['job']['id'], status='stopped')
        with patch.object(svc, 'get_executor', return_value=executor):
            self.assertTrue(svc.submit_registration_resume(3624)['created'])

    def test_retry_routing_including_batch_summary_and_parent_link(self):
        job = db.create_job('generic_api')
        db.update_job(job['id'], email=self.account['email'], status='failed')
        source = db.get_job(job['id'])
        self.assertEqual(svc.get_retry_info(source)['retry_action'], 'registration_resume')
        svc.enrich_jobs_retry_info([source])
        self.assertEqual(source['retry_label'], '继续注册')
        with patch.object(svc, 'get_executor', return_value=MagicMock()):
            result = svc.retry_job(job['id'])
        self.assertEqual(result['job']['job_type'], 'registration_resume')
        self.assertEqual(result['job']['parent_job_id'], job['id'])
        self.assertEqual(result['job']['account_id'], 3624)

    def test_worker_uses_saved_email_not_global_config_or_new_pool(self):
        job, _ = db.create_registration_resume_job(3624)
        result = {'success': True, 'email': self.account['email'], 'account_id': 3624}
        with patch('main.run_registration', return_value=result) as run, \
             patch.object(svc, '_prepare_registration_args', side_effect=AssertionError('不得领取新邮箱')):
            svc._run_one_job(job['id'], job['log_file'])
        self.assertEqual(run.call_args.kwargs['email'], self.account['email'])
        self.assertTrue(run.call_args.kwargs['resume_registration'])
        self.assertEqual(db.get_job(job['id'])['status'], 'success')

    def test_resume_worker_does_not_disable_mailbox_for_login_failure(self):
        job, _ = db.create_registration_resume_job(3624)
        result = {'success': False, 'email': self.account['email'],
                  'error': '登录失败: https://auth.openai.com/log-in/password'}
        with patch('main.run_registration', return_value=result), \
             patch.object(svc, '_disable_job_email') as disable:
            svc._run_one_job(job['id'], job['log_file'])
        disable.assert_not_called()
        self.assertEqual(db.get_job(job['id'])['status'], 'failed')
        self.assertTrue(db._matches_codex_status_filter(db.get_account(3624), 'password_registered'))
        self.assertTrue(svc.get_retry_info(db.get_job(job['id']))['retryable'])

    def test_rejects_missing_complete_and_deactivated_accounts(self):
        self.assertEqual(svc.submit_registration_resume(999)['status'], 404)
        for field, value in [('extra_json', '{"registration_stage":"complete"}'),
                             ('live_check_status', 'deactivated')]:
            db._save_accounts([{**self.account, field: value}])
            self.assertEqual(svc.submit_registration_resume(3624)['status'], 409)

    def test_submission_failure_marks_job_failed(self):
        executor = MagicMock()
        executor.submit.side_effect = RuntimeError('closed')
        with patch.object(svc, 'get_executor', return_value=executor):
            result = svc.submit_registration_resume(3624)
        self.assertEqual(result['status'], 500)
        job, created = db.create_registration_resume_job(3624)
        self.assertTrue(created)

    def test_api_and_compact_stage(self):
        compact = _compact_account_for_list(self.account)
        self.assertEqual(compact['registration_stage'], 'password_registered')
        self.assertNotIn('extra_json', compact)
        client = create_app(auth_code='test-auth').test_client()
        client.environ_base['HTTP_X_AUTH_CODE'] = 'test-auth'
        with patch.object(svc, 'get_executor', return_value=MagicMock()):
            response = client.post('/api/accounts/3624/continue-registration')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        response = client.post('/api/codex/retry', json={'email': self.account['email']})
        self.assertEqual(response.status_code, 409)


    def test_bulk_mixed_accounts_duplicates_and_existing_task(self):
        db._save_accounts([self.account,
                           {**self.account, 'id': 2, 'email': 'complete@example.com', 'extra_json': '{"registration_stage":"complete"}'},
                           {**self.account, 'id': 3, 'email': 'dead@example.com', 'live_check_status': 'deactivated'}])
        executor = MagicMock()
        with patch.object(svc, 'get_executor', return_value=executor):
            result = svc.submit_registration_resume_bulk([3624, '3624', 2, 3, 999, 'bad', True, 1.2], workers=3)
            again = svc.submit_registration_resume_bulk([3624], workers=3)
        self.assertEqual(result['created_count'], 1)
        self.assertEqual(result['skipped_count'], 6)
        self.assertEqual(result['failed_count'], 0)
        self.assertEqual(again['reused_count'], 1)
        self.assertEqual(again['created_count'], 0)
        executor.submit.assert_called_once()

    def test_bulk_validation_rejects_before_creating_jobs(self):
        for ids, workers in [([], 1), ('bad', 1), ([3624] * 501, 1),
                             ([3624], 0), ([3624], 17), ([3624], 1.5), ([3624], True), ([3624], None)]:
            with self.subTest(ids=type(ids), workers=workers), patch.object(svc, 'submit_registration_resume') as submit:
                self.assertEqual(svc.submit_registration_resume_bulk(ids, workers)['status'], 400)
                submit.assert_not_called()

    def test_bulk_failure_does_not_abort_other_accounts(self):
        outcomes = [RuntimeError('db busy'), {'ok': False, 'status': 500, 'error': 'queue closed'},
                    {'ok': True, 'created': True, 'job': {'id': 3, 'email': 'ok@example.com'}}]
        with patch.object(svc, 'submit_registration_resume', side_effect=outcomes) as submit:
            result = svc.submit_registration_resume_bulk([1, 2, 3], workers=4)
        self.assertEqual(result['failed_count'], 2)
        self.assertEqual(result['created_count'], 1)
        self.assertEqual(submit.call_args.kwargs['workers'], 4)

    def test_bulk_api(self):
        client = create_app(auth_code='test-auth').test_client()
        client.environ_base['HTTP_X_AUTH_CODE'] = 'test-auth'
        for body in ([], {}, {'account_ids': [3624], 'workers': 99}):
            self.assertEqual(client.post('/api/accounts/continue-registration-bulk', json=body).status_code, 400)
        with patch.object(svc, 'get_executor', return_value=MagicMock()):
            response = client.post('/api/accounts/continue-registration-bulk', json={'account_ids': [3624, 999], 'workers': 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['created_count'], 1)
        self.assertEqual(response.get_json()['skipped_count'], 1)


class ResumeProtocolChainTests(unittest.TestCase):
    def setUp(self):
        from tests.test_protocol_registration_alignment import RegistrationChainTests
        RegistrationChainTests.setUp(self)

    def test_resume_password_rejected_preserves_mailbox_and_saved_account(self):
        import main
        self.patches['follow_authorize'].return_value = 'https://auth.openai.com/log-in/password'
        self.patches['continue_existing_login'].side_effect = RuntimeError(
            '/api/accounts/password/verify 认证失败: status=401, code=invalid_password, flow=password_verify')
        result = main.run_registration('a@example.com', 'Test User', '1990-01-01', resume_registration=True)
        self.assertFalse(result['success'])
        self.assertIn('invalid_password', result['error'])
        self.release.assert_not_called()
        self.patches['register_user'].assert_not_called()

    def test_resume_explicit_deactivation_still_marks_mailbox_failed(self):
        import main
        from core.openai_auth import AccountUnusableError
        self.patches['follow_authorize'].return_value = 'https://auth.openai.com/log-in/password'
        self.patches['continue_existing_login'].side_effect = AccountUnusableError(
            '账号已废弃', error_code='account_deactivated')
        result = main.run_registration('a@example.com', 'Test User', '1990-01-01', resume_registration=True)
        self.assertFalse(result['success'])
        self.assertEqual(self.release.call_args.kwargs['status'], 'failed')

    def test_resume_never_registers_password_again_and_completes_about_you(self):
        import main
        p = self.patches
        p['continue_existing_login'].return_value = ('saved-password', {
            'page': {'type': 'email_otp_verification'},
            'continue_url': '/email-verification', '_otp_already_sent': True})
        with patch.object(main._roxy_cfg, 'REGISTRATION_DRIVER', 'roxy'):
            result = main.run_registration('a@example.com', 'Test User', '1990-01-01',
                                           otp_code='123456', resume_registration=True)
        self.assertTrue(result['success'])
        p['register_user'].assert_not_called()
        p['generate_registration_password'].assert_not_called()
        p['continue_existing_login'].assert_called_once()
        p['create_account'].assert_called_once()
        self.assertEqual(p['save_account_data'].call_args.kwargs['extra']['registration_stage'], 'complete')
        self.assertEqual(p['save_account_data'].call_args.kwargs['extra']['registration_password'], 'saved-password')

    def test_resume_email_verification_preserves_current_challenge(self):
        import main
        from core import protocol_registration as flow
        p = self.patches
        p['continue_existing_login'].side_effect = flow.continue_existing_login
        account = {'extra_json': json.dumps({'registration_password': 'saved-password'})}
        with patch.object(db, 'get_account_by_email', return_value=account):
            result = main.run_registration('a@example.com', 'Test User', '1990-01-01',
                                           otp_code='123456', resume_registration=True)
        self.assertTrue(result['success'])
        self.assertEqual(p['continue_existing_login'].call_args.args[2], 'existing_otp')
        p['BrowserSession'].return_value.post.assert_not_called()
        p['register_user'].assert_not_called()
        p['generate_registration_password'].assert_not_called()
        p['validate_email_otp'].assert_called_once()
        self.assertEqual(p['save_account_data'].call_args.kwargs['extra']['registration_password'], 'saved-password')

    def test_resume_explicit_signup_reuses_saved_password(self):
        import main
        p = self.patches
        p['follow_authorize'].return_value = 'https://auth.openai.com/create-account/password'
        account = {'extra_json': json.dumps({'registration_password': 'saved-password'})}
        with patch.object(db, 'get_account_by_email', return_value=account):
            result = main.run_registration('a@example.com', 'Test User', '1990-01-01',
                                           otp_code='123456', resume_registration=True)
        self.assertTrue(result['success'])
        self.assertEqual(p['register_user'].call_args.args[2], 'saved-password')
        p['generate_registration_password'].assert_not_called()
        p['continue_existing_login'].assert_not_called()
        p['checkpoint_registration_password'].assert_called_once()

    def test_resume_explicit_signup_without_password_stops_before_submit(self):
        import main
        p = self.patches
        p['follow_authorize'].return_value = 'https://auth.openai.com/create-account/password'
        with patch.object(db, 'get_account_by_email', return_value={'password': 'mailbox-password'}):
            result = main.run_registration('a@example.com', 'Test User', '1990-01-01',
                                           otp_code='123456', resume_registration=True)
        self.assertFalse(result['success'])
        self.assertIn('缺少已保存的注册密码', result['error'])
        p['register_user'].assert_not_called()
        p['generate_registration_password'].assert_not_called()
        self.release.assert_not_called()
