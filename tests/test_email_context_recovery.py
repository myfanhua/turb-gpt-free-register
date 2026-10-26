"""逐来源验证清空进程缓存后的恢复；仅临时 SQLite 和假取件响应。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import db, email_provider as provider, protocol_registration as registration, account_export
from core import cf_temp_mail_client as cf, xigua_client as xigua, remail_client as remail
from core import generic_api_mail_client as generic, imap_mail_client as imap, outlook_client as outlook
from core import gptmail_client as gptmail, mailnest_client as mailnest, cloudmail_client as cloudmail, mailcode_client as mailcode
from tests import test_protocol_registration_audit as audit


EMAIL = 'a@example.test'
MESSAGE = {'id': 'm1', 'from': 'noreply@openai.com', 'subject': 'Your OpenAI code 654321',
           'text': 'Your verification code is 654321', 'timestamp': 200}


class EmailContextRecoveryTests(unittest.TestCase):
    setUp = audit.RegistrationCheckpointTests.setUp

    def seed(self, source, metadata):
        db.checkpoint_registration_account(email=EMAIL, access_token='token',
            email_source=source, extra={'email_service': metadata})

    def cf_context(self):
        return cf.CFTempMailAccount(EMAIL, 'private-jwt', 'example.test', 100)

    def test_cloudflare_final_save_then_cache_clear_restores_original_jwt(self):
        with patch.dict(cf._CONTEXT_CACHE, {EMAIL: self.cf_context()}, clear=True), \
             patch.object(cf, '_base_url', return_value='https://worker.example.test'):
            account_export.save_account_data(EMAIL, 'token', email_source='cloudflare',
                                             auto_twofa=False, auto_plan_check=False)
            cf._CONTEXT_CACHE.clear()
            with patch.object(cf, '_request', side_effect=AssertionError('must not create new mailbox')):
                restored = cf.restore_account_context(' A@EXAMPLE.TEST ')
            self.assertEqual(restored.jwt, 'private-jwt')
            self.assertEqual(restored.email.casefold(), EMAIL)

    def test_cloudflare_restored_jwt_is_used_by_real_otp_polling(self):
        with patch.dict(cf._CONTEXT_CACHE, {EMAIL: self.cf_context()}, clear=True), \
             patch.object(cf, '_base_url', return_value='https://worker.example.test'):
            self.seed('cloudflare', provider.get_email_context_metadata(EMAIL, 'cloudflare'))
            cf._CONTEXT_CACHE.clear()
            with patch.object(cf, 'list_messages', return_value=[MESSAGE]) as messages:
                self.assertEqual(cf.fetch_latest_otp(EMAIL, after_ts=150, max_wait=1, settle_seconds=0), '654321')
            messages.assert_called_once_with('private-jwt')

    def test_password_checkpoint_captures_cloudflare_jwt(self):
        session = SimpleNamespace(device_id='d', browser_profile={}, sentinel_sid='s', proxy='')
        with patch.dict(cf._CONTEXT_CACHE, {EMAIL: self.cf_context()}, clear=True), \
             patch.object(cf, '_base_url', return_value='https://worker.example.test'), \
             patch.object(provider, 'resolve_email_source', return_value='cloudflare'):
            registration.checkpoint_registration_password(session, EMAIL, 'password')
        row = db.get_account_by_email(EMAIL)
        self.assertEqual(row['email_source'], 'cloudflare')
        self.assertEqual(json.loads(row['extra_json'])['email_service']['jwt'], 'private-jwt')

    def test_cloudflare_restore_rejects_other_source_email_or_worker(self):
        metadata = {'source': 'cloudflare', 'email': EMAIL, 'jwt': 'private-jwt', 'api_base': 'https://worker.example.test'}
        cases = [
            ('remail', metadata),
            ('cloudflare', {**metadata, 'email': 'other@example.test'}),
            ('cloudflare', {**metadata, 'source': 'remail'}),
            ('cloudflare', {**metadata, 'api_base': 'https://other-worker.example.test'}),
            ('cloudflare', {**metadata, 'jwt': ''}),
        ]
        with patch.dict(cf._CONTEXT_CACHE, {}, clear=True), patch.object(cf, '_base_url', return_value='https://worker.example.test'):
            for source, saved in cases:
                with self.subTest(source=source, saved=saved):
                    self.seed(source, saved)
                    self.assertIsNone(cf.restore_account_context(EMAIL))

    def test_missing_legacy_cloudflare_jwt_does_not_generate_new_address(self):
        with patch.dict(cf._CONTEXT_CACHE, {}, clear=True), \
             patch.object(cf, '_request', side_effect=AssertionError('no network')), \
             patch.object(cf, 'create_address', side_effect=AssertionError('no replacement mailbox')):
            with self.assertRaisesRegex(cf.CFTempMailError, '取件凭证缺失'):
                cf.fetch_latest_otp(EMAIL, max_wait=1)

    def test_cache_clear_does_not_erase_existing_recovery_metadata_on_save(self):
        metadata = {'source': 'cloudflare', 'email': EMAIL, 'jwt': 'saved', 'api_base': 'https://worker.example.test'}
        self.seed('cloudflare', metadata)
        with patch.dict(cf._CONTEXT_CACHE, {}, clear=True):
            account_export.save_account_data(EMAIL, 'new-token', email_source='cloudflare',
                                             auto_twofa=False, auto_plan_check=False)
        self.assertEqual(json.loads(db.get_account_by_email(EMAIL)['extra_json'])['email_service'], metadata)

    def test_xigua_final_save_restores_exact_order_without_listing_or_buying(self):
        with patch.dict(xigua._CACHE, {EMAIL: xigua.XiguaAccount(EMAIL, 'exact-order')}, clear=True):
            account_export.save_account_data(EMAIL, 'token', email_source='xigua',
                                             auto_twofa=False, auto_plan_check=False)
            xigua._CACHE.clear()
            with patch.object(xigua, '_request', side_effect=AssertionError('no orders lookup')):
                self.assertEqual(xigua.restore_account_context(EMAIL).order_id, 'exact-order')

    def test_xigua_polling_uses_saved_order_after_cache_clear(self):
        with patch.dict(xigua._CACHE, {EMAIL: xigua.XiguaAccount(EMAIL, 'exact-order')}, clear=True), \
             patch.dict(xigua._LAST_CODES, {}, clear=True):
            self.seed('xigua', provider.get_email_context_metadata(EMAIL, 'xigua'))
            xigua._CACHE.clear()
            with patch.object(xigua, '_request', return_value={'code': '654321'}) as request, \
                 patch.object(xigua.time, 'sleep'):
                self.assertEqual(xigua.fetch_latest_otp(EMAIL, max_wait=1), '654321')
            self.assertEqual(request.call_count, 2)
            self.assertTrue(all(c.args == ('GET', '/api/v1/orders/exact-order') for c in request.call_args_list))

    def test_xigua_legacy_account_retains_read_only_order_lookup(self):
        with patch.dict(xigua._CACHE, {}, clear=True), \
             patch.object(xigua, '_request', return_value={'data': {'items': [
                 None, {'email': 'other@example.test', 'id': 'wrong'}, {'email': EMAIL.upper(), 'id': 'right'}
             ]}}) as request:
            self.assertEqual(xigua.restore_account_context(EMAIL).order_id, 'right')
        request.assert_called_once_with('GET', '/api/v1/orders')

    def test_xigua_changed_api_base_does_not_send_original_order(self):
        self.seed('xigua', {'source': 'xigua', 'email': EMAIL, 'order_id': 'saved', 'api_base': 'https://old.example.test'})
        with patch.dict(xigua._CACHE, {}, clear=True), patch.object(xigua, '_request') as request:
            with self.assertRaisesRegex(xigua.XiguaError, '地址已变更'):
                xigua.restore_account_context(EMAIL)
        request.assert_not_called()

    def test_remail_existing_metadata_format_is_preserved(self):
        context = remail.RemailAccount(EMAIL, 'service-token', 'order', 1, 'example.test')
        with patch.dict(remail._CONTEXT_CACHE, {EMAIL: context}, clear=True):
            saved = provider.get_email_context_metadata(EMAIL, 'REMAIL')
        self.assertEqual(saved['service_token'], 'service-token')
        self.assertEqual(saved['order_no'], 'order')

    def test_existing_pool_sources_restore_without_extra_account_snapshot(self):
        db.import_generic_api_emails([{'email': EMAIL, 'code_url': 'https://inbox.example.test/code'}])
        db.import_imap_emails([{'email': EMAIL, 'imap_password': 'imap-pw', 'imap_server': 'imap.example.test'}])
        db.import_outlook_accounts([{'email': EMAIL, 'password': 'mail-pw', 'client_id': 'cid', 'refresh_token': 'rt'}])
        with patch.dict(generic._CONTEXT_CACHE, {}, clear=True), patch.dict(imap._CONTEXT_CACHE, {}, clear=True), \
             patch.dict(outlook._CONTEXT_CACHE, {}, clear=True):
            self.assertEqual(generic.get_account_context(EMAIL).code_url, 'https://inbox.example.test/code')
            self.assertEqual(imap.get_account_context(EMAIL).password, 'imap-pw')
            self.assertEqual(outlook.get_account_context(EMAIL).refresh_token, 'rt')

    def test_gptmail_polling_needs_no_creation_context(self):
        with patch.dict(gptmail._CONTEXT_CACHE, {}, clear=True), \
             patch.object(gptmail, '_get', side_effect=[{'emails': [MESSAGE]}, MESSAGE]):
            self.assertEqual(gptmail.fetch_latest_otp(EMAIL, after_ts=150, max_wait=1, settle_seconds=0), '654321')

    def test_mailnest_polling_needs_no_project_creation_context(self):
        with patch.dict(mailnest._CONTEXT_CACHE, {}, clear=True), \
             patch.object(mailnest, '_get_mails', return_value=[MESSAGE]) as receive:
            self.assertEqual(mailnest.fetch_latest_otp(EMAIL, after_ts=150, max_wait=1, settle_seconds=0), '654321')
        receive.assert_called_once_with(EMAIL)

    def test_cloudmail_polling_needs_no_random_user_password(self):
        with patch.dict(cloudmail._CONTEXT_CACHE, {}, clear=True), \
             patch.object(cloudmail, '_request', return_value=[MESSAGE]) as receive:
            self.assertEqual(cloudmail.fetch_latest_otp(EMAIL, after_ts=150, max_wait=1, settle_seconds=0), '654321')
        self.assertEqual(receive.call_args.args[1]['toEmail'], EMAIL)

    def test_mailcode_has_disk_context_restoration_without_purchase(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(mailcode, '_STATE_DIR', Path(directory)), \
             patch.object(mailcode, '_key', return_value='test-key'), \
             patch.dict(mailcode._CONTEXT_CACHE, {}, clear=True), \
             patch.object(mailcode, '_request', side_effect=AssertionError('no network')):
            account = mailcode.MailCodeAccount(EMAIL, 'mailbox-1', 'order-1', '7')
            mailcode._save_context(account)
            restored = mailcode._restore(EMAIL)
            self.assertEqual((restored.mailbox_id, restored.next_sequence), ('mailbox-1', '7'))

    def test_domain_mailbox_uses_shared_imap_without_creation_context(self):
        from core import qqmail_client as qqmail
        mail = MagicMock()
        record = {**MESSAGE, 'to': EMAIL, 'date': '1970-01-01T00:03:20+00:00'}
        with patch.object(qqmail, '_connect_imap', return_value=mail), \
             patch.object(qqmail, '_search_messages', return_value=[record]):
            self.assertEqual(qqmail.fetch_latest_otp(EMAIL, after_ts=150, max_wait=1, settle_seconds=0), '654321')
        mail.logout.assert_called_once()

    def test_stateless_and_durable_sources_do_not_copy_global_api_keys(self):
        for source in ('gptmail', 'mailnest', 'cloudmail', 'outlook', 'generic_api', 'imap', 'mailcode', 'cloudflare_domain'):
            self.assertEqual(provider.get_email_context_metadata(EMAIL, source), {})

    def test_malformed_saved_metadata_is_ignored(self):
        self.seed('cloudflare', {})
        with db._sqlite_conn() as conn:
            row = db.get_account_by_email(EMAIL)
            row['extra_json'] = '{broken'
            conn.execute('UPDATE accounts SET payload=? WHERE id=?', (json.dumps(row), row['id']))
        self.assertEqual(provider.get_saved_email_context_metadata(EMAIL, 'cloudflare'), {})

    def test_metadata_errors_do_not_log_supplier_secrets(self):
        with patch.object(cf, 'get_account_context_metadata', side_effect=RuntimeError('private-jwt')), \
             self.assertLogs(provider.logger, level='WARNING') as captured:
            self.assertEqual(provider.get_email_context_metadata(EMAIL, 'cloudflare'), {})
        self.assertNotIn('private-jwt', str(captured.output))
