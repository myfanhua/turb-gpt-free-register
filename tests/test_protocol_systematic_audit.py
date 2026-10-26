"""系统审计新增回归：边界停止、最终单行落库、事务一致性。"""
import json
import importlib
import sqlite3
import sys
import time
import unittest
from contextlib import ExitStack, closing
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import db, email_provider, manual_otp, flow_trigger
from core import registration_service as svc
from core.registration_service import StopRequested
from core.session import BrowserSession
from tests import test_protocol_registration_audit as _checkpoint_tests


class BoundaryStopTests(unittest.TestCase):
    def test_email_dispatch_does_not_swallow_stop_and_buy_another_source(self):
        with patch.object(email_provider, 'parse_email_sources', return_value=['remail', 'cloudmail']), \
             patch.object(email_provider, '_pick_from_source', side_effect=[StopRequested('stop'), 'b@example.test']) as pick:
            with self.assertRaises(StopRequested):
                email_provider.acquire_email()
        self.assertEqual(pick.call_count, 1)

    def test_transport_boundary_blocks_all_backends_before_io(self):
        for backend in ('curl_cffi', 'wreq'):
            for method in ('get', 'post'):
                with self.subTest(backend=backend, method=method):
                    session = object.__new__(BrowserSession)
                    session.http_client = backend
                    session.session = MagicMock()
                    session.blocked_until = 0
                    with patch('core.registration_service.check_stop_requested', side_effect=StopRequested('stop')):
                        with self.assertRaises(StopRequested):
                            getattr(session, method)('https://example.test/')
                    session.session.get.assert_not_called()
                    session.session.post.assert_not_called()

    def test_manual_otp_does_not_swallow_stop(self):
        with patch.object(manual_otp, '_event_for', return_value=MagicMock()), \
             patch.object(manual_otp.sys if hasattr(manual_otp, 'sys') else sys, 'stdin', None), \
             patch('core.registration_service.check_stop_requested', side_effect=StopRequested('stop')), \
             patch.object(manual_otp.time, 'time', side_effect=[1, 1, 1, 1000]):
            with self.assertRaises(StopRequested):
                manual_otp.wait_for_manual_otp('stop@example.test', timeout=10, job_id=1)
        self.assertNotIn('stop@example.test', [row['email'] for row in manual_otp.list_waiting()])

    def test_flow_does_not_send_after_stop(self):
        with patch('config.flow_trigger.ENABLE_FLOW_TRIGGER', True), \
             patch('core.registration_service.check_stop_requested', side_effect=StopRequested('stop')), \
             patch.object(flow_trigger.requests, 'post') as request:
            with self.assertRaises(StopRequested):
                flow_trigger.trigger_flow('test-token')
        request.assert_not_called()

    def test_all_eleven_mail_sources_stop_before_polling(self):
        for name in ('outlook', 'cf_temp_mail', 'cloudmail', 'remail', 'xigua', 'gptmail',
                     'mailnest', 'mailcode', 'generic_api_mail', 'imap_mail', 'qqmail'):
            module = importlib.import_module('core.' + name + '_client')
            with self.subTest(source=name), \
                 patch('core.registration_service.check_stop_requested', side_effect=StopRequested('stop')):
                with self.assertRaises(StopRequested):
                    module.fetch_latest_otp('stop@example.test', after_ts=0)

    def test_manual_web_job_never_reads_terminal_and_cleans_wait_state(self):
        with patch.object(sys, 'stdin', SimpleNamespace(isatty=lambda: True)), \
             patch('builtins.input', side_effect=AssertionError('Web worker must not read stdin')) as read, \
             patch.object(manual_otp, '_event_for', return_value=MagicMock()), \
             patch('core.registration_service.check_stop_requested', side_effect=[None, None, StopRequested('stop')]):
            with self.assertRaises(StopRequested):
                manual_otp.wait_for_manual_otp('web-stop@example.test', job_id=1)
        read.assert_not_called()
        self.assertNotIn('web-stop@example.test', [row['email'] for row in manual_otp.list_waiting()])

    def test_shared_poll_sleep_exits_during_active_job_wait(self):
        from core import task_control, registration_service as svc
        started = time.monotonic()
        def check():
            if time.monotonic() - started >= .05:
                raise StopRequested('stop')
        with patch.object(svc, '_THREAD_CTX', SimpleNamespace(job_id=1)), \
             patch.object(svc, 'check_stop_requested', side_effect=check):
            with self.assertRaises(StopRequested):
                task_control.sleep(60)
        self.assertLess(time.monotonic() - started, 1)

    def test_shared_sleep_preserves_non_job_single_sleep(self):
        from core import task_control, registration_service as svc
        with patch.object(svc, '_THREAD_CTX', SimpleNamespace()), \
             patch.object(task_control.time, 'sleep') as sleep:
            task_control.sleep(6)
        sleep.assert_called_once_with(6)

    def test_cancelled_sms_acquisition_does_not_create_client_or_buy_number(self):
        from core import sms_provider
        with patch('core.registration_service.check_stop_requested', side_effect=StopRequested('stop')), \
             patch.object(sms_provider, '_http') as client:
            with self.assertRaises(StopRequested):
                sms_provider.acquire_number()
        client.assert_not_called()

    def test_transport_returns_remote_result_before_next_stop_check(self):
        # 远端写入成功后必须先让调用方保存检查点，不能用响应后的停止检查丢弃响应。
        session = object.__new__(BrowserSession)
        session.session = MagicMock()
        session.blocked_until = 0
        response = SimpleNamespace(status_code=200)
        session.session.post.return_value = response
        with patch.object(session, '_observe_response_for_circuit_breaker', return_value=response), \
             patch('core.registration_service.check_stop_requested', side_effect=[None, StopRequested('stop')]) as check:
            self.assertIs(session.post('https://example.test/write'), response)
        check.assert_called_once()


class FinalSaveTests(unittest.TestCase):
    setUp = _checkpoint_tests.RegistrationCheckpointTests.setUp
    put = _checkpoint_tests.RegistrationCheckpointTests.put

    def test_final_save_does_not_load_or_rewrite_collections(self):
        self.put(extra={'registration_password': 'p', 'historical': 1})
        with patch.object(db, '_load_accounts', side_effect=AssertionError('full account read')), \
             patch.object(db, '_load_outlook', side_effect=AssertionError('full pool read')), \
             patch.object(db, '_save_accounts', side_effect=AssertionError('full account rewrite')), \
             patch.object(db, '_save_outlook', side_effect=AssertionError('full pool rewrite')):
            rid = db.insert_account(email='a@example.test', access_token='final', extra={'registration_stage': 'complete'})
        row = db.get_account(rid)
        self.assertEqual(row['access_token'], 'final')
        self.assertEqual(json.loads(row['extra_json'])['historical'], 1)

    def test_final_save_updates_only_matching_account_and_outlook_material(self):
        unrelated = self.put(email='other@example.test', extra={'unchanged': True})
        before = db.get_account(unrelated)
        db._save_outlook([{'id': 41, 'email': 'A@example.test', 'password': 'mail-password',
                           'client_id': 'client', 'refresh_token': 'refresh', 'status': 'available'},
                          {'id': 42, 'email': 'untouched@example.test', 'status': 'available'}])
        rid = db.insert_account(email='a@example.test', access_token='final', totp_secret='totp',
                                extra={'registration_stage': 'complete', 'registration_password': 'gpt-password'})
        account = db.get_account(rid)
        self.assertEqual(account['password'], 'mail-password')
        self.assertEqual(account['refresh_token'], 'refresh')
        self.assertEqual(db.get_account(unrelated), before)
        with closing(db._sqlite_conn()) as conn:
            rows = [json.loads(row['payload']) for row in conn.execute('SELECT payload FROM email_pool ORDER BY id')]
        self.assertEqual(rows[0]['registered_account_id'], rid)
        self.assertEqual(rows[0]['access_token'], 'final')
        self.assertEqual(rows[0]['status'], 'used')
        self.assertTrue(rows[0]['updated_at'])
        self.assertEqual(rows[1], {'id': 42, 'email': 'untouched@example.test', 'status': 'available'})

    def test_final_save_rolls_back_account_if_pool_write_fails(self):
        db._save_outlook([{'id': 1, 'email': 'a@example.test', 'status': 'available'}])
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("CREATE TRIGGER fail_pool BEFORE UPDATE ON email_pool BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert_account(email='a@example.test', access_token='final')
        self.assertIsNone(db.get_account_by_email('a@example.test'))
        with closing(db._sqlite_conn()) as conn:
            row = json.loads(conn.execute('SELECT payload FROM email_pool WHERE id=1').fetchone()[0])
        self.assertEqual(row['status'], 'available')

    def test_final_save_uses_email_indexes(self):
        with closing(db._sqlite_conn()) as conn:
            for sql, args, index in (
                ('SELECT payload FROM accounts WHERE id=? OR account_email_lower(email)=? ORDER BY id LIMIT 1',
                 (None, 'a@example.test'), 'idx_accounts_email_lower'),
                ('SELECT payload FROM email_pool WHERE account_email_lower(email)=? AND source=? ORDER BY id LIMIT 1',
                 ('a@example.test', 'outlook'), 'idx_email_pool_email_lower')):
                detail = ' '.join(str(tuple(row)) for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, args))
                self.assertIn(index, detail)

    def test_final_save_merges_concurrent_same_email_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda index: db.insert_account(
                email='a@example.test', access_token='token',
                extra={'registration_stage': 'complete', f'field_{index}': index}), range(8)))
        self.assertEqual(len(set(ids)), 1)
        extra = json.loads(db.get_account(ids[0])['extra_json'])
        self.assertTrue(all(extra[f'field_{index}'] == index for index in range(8)))

    def test_explicit_codex_success_clears_old_error_but_omitted_status_preserves_it(self):
        rid = db.insert_account(email='a@example.test', access_token='token',
                                codex_status='failed', codex_error='old failure')
        db.insert_account(email='a@example.test', access_token='token')
        self.assertEqual(db.get_account(rid)['codex_error'], 'old failure')
        db.insert_account(email='a@example.test', access_token='token', codex_status='success')
        self.assertIsNone(db.get_account(rid)['codex_error'])


class JobFinalizationTests(unittest.TestCase):
    setUp = _checkpoint_tests.RegistrationCheckpointTests.setUp

    def test_stop_arriving_between_result_check_and_commit_wins(self):
        job = db.create_job('outlook')
        def late_stop(job_id):
            svc._STOP_EVENTS[job_id].set()
            db.update_job(job_id, status='stopping')
            return False  # 模拟前一次停止检查返回后停止才到达。
        with patch.object(svc, '_JobLogContext'), \
             patch.object(svc, '_prepare_registration_args', return_value=('a@example.test', 'Demo', '1990-01-01')), \
             patch.object(svc, 'is_stop_requested', side_effect=late_stop), \
             patch('main.run_registration', return_value={'success': True, 'email': 'a@example.test', 'account_id': 123}):
            svc._run_one_job(job['id'], job['log_file'])
        result = db.get_job(job['id'])
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['account_id'], 123)
        self.assertNotIn(job['id'], svc._ACTIVE_JOBS)

    def test_database_stop_without_local_event_wins(self):
        job = db.create_job('outlook')
        db.update_job(job['id'], status='stopping')
        self.assertEqual(svc._finish_active_job(job['id'], status='success'), 'stopped')
        self.assertEqual(db.get_job(job['id'])['status'], 'stopped')

    def test_finalization_cannot_overwrite_terminal_state(self):
        job = db.create_job('outlook')
        db.update_job(job['id'], status='cancelled')
        self.assertIsNone(svc._finish_active_job(job['id'], status='success'))
        self.assertEqual(db.get_job(job['id'])['status'], 'cancelled')

    def test_codex_retry_typed_stop_is_not_failed_and_releases_reservation(self):
        job = db.create_job('outlook')
        with patch.object(svc.codex_retry_service, 'run_worker', side_effect=StopRequested('stop')), \
             patch.object(svc.codex_retry_service, 'release') as release:
            svc._run_codex_retry_job(job['id'], job['log_file'], 'a@example.test', 1)
        self.assertEqual(db.get_job(job['id'])['status'], 'stopped')
        release.assert_called_once_with('a@example.test')
        self.assertNotIn(job['id'], svc._ACTIVE_JOBS)

    def test_poll_sleep_observes_database_stop_without_local_event(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from core.task_control import sleep
        job = db.create_job('outlook')
        db.update_job(job['id'], status='running')
        started = threading.Event()
        def wait():
            svc._THREAD_CTX.job_id = job['id']
            try:
                started.set()
                sleep(60)
            finally:
                del svc._THREAD_CTX.job_id
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait)
            self.assertTrue(started.wait(1))
            db.update_job(job['id'], status='stopping')
            with self.assertRaises(StopRequested):
                future.result(timeout=1)
