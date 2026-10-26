"""独立复核：异常清理不遮蔽停止、typed stop、Cookie同名冲突与并发隔离。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from requests.cookies import RequestsCookieJar, create_cookie
from config import openai_protocol as settings
from core import openai_auth as auth, sentinel_runner as runner, sentinel_sdk as cache
from core.registration_service import StopRequested
from core.session import BrowserSession
from tests.test_sentinel_target_executor import FAKE_SDK


class ErrorAndStopReviewTests(unittest.TestCase):
    def test_stop_with_retry_keywords_is_not_transient(self):
        error=StopRequested('connection timeout HTTP 403 proxy')
        error.response=SimpleNamespace(status_code=403)
        self.assertFalse(auth._is_transient_network_error(error))
        self.assertFalse(auth._is_retryable_authorize_error(error))

    def test_request_retry_does_not_retry_typed_stop(self):
        error=StopRequested('connection timeout HTTP 403 proxy')
        error.response=SimpleNamespace(status_code=403)
        fn=MagicMock(side_effect=error)
        with patch.object(auth,'_check_stop_requested'), patch.object(auth,'_interruptible_sleep') as sleep:
            with self.assertRaises(StopRequested):
                auth._request_with_proxy_retry(MagicMock(),'offline',fn)
        fn.assert_called_once()
        sleep.assert_not_called()

    def test_authorize_http_status_path_does_not_swallow_stop(self):
        error=StopRequested('stop')
        error.response=SimpleNamespace(status_code=403)
        session=MagicMock()
        session.get.side_effect=error
        with patch.object(auth,'_check_stop_requested'), patch.object(auth,'_interruptible_sleep') as sleep:
            with self.assertRaises(StopRequested):
                auth.follow_authorize(session,'https://auth.openai.com/offline')
        session.get.assert_called_once()
        sleep.assert_not_called()

    def test_cleanup_does_not_decode_bad_utf8_or_mask_stop(self):
        started=time.monotonic()
        def check():
            if time.monotonic()-started > .12:
                raise StopRequested('stop')
        code="import sys,time;sys.stdout.buffer.write(b'\\xff');sys.stdout.buffer.flush();time.sleep(60)"
        with patch.object(auth,'_check_stop_requested',side_effect=check):
            with self.assertRaises(StopRequested):
                runner._run_node([sys.executable,'-c',code],input='',cwd=str(runner._PROJECT_ROOT),
                                 timeout=60,env=os.environ.copy())
        self.assertLess(time.monotonic()-started,2)

    def test_cleanup_preserves_timeout_when_stdout_is_invalid_utf8(self):
        code="import sys,time;sys.stdout.buffer.write(b'\\xff');sys.stdout.buffer.flush();time.sleep(60)"
        with patch.object(auth,'_check_stop_requested'):
            with self.assertRaises(subprocess.TimeoutExpired):
                runner._run_node([sys.executable,'-c',code],input='',cwd=str(runner._PROJECT_ROOT),
                                 timeout=.2,env=os.environ.copy())

    def test_so_error_keys_are_rejected_even_with_empty_values(self):
        for key in ('e','error'):
            for value in ('',None,False):
                data={'so':'observer','c':'c','id':'d','flow':'f',key:value}
                with self.subTest(key=key,value=value),self.assertRaisesRegex(RuntimeError,'返回错误'):
                    auth._format_sentinel_so_header(json.dumps(data),challenge='c',device_id='d',flow='f')


class CookieAndCacheReviewTests(unittest.TestCase):
    def session(self):
        session=object.__new__(BrowserSession)
        session.device_id='authoritative-device'
        session.browser_profile={'navigator_language':'ja-JP'}
        session.session=SimpleNamespace(cookies=SimpleNamespace(jar=RequestsCookieJar()))
        return session

    def test_js_exports_only_one_authoritative_device_and_locale(self):
        session=self.session()
        jar=session.session.cookies.jar
        for domain, value in (('auth.openai.com','old-host'),('.openai.com','old-parent')):
            jar.set_cookie(create_cookie('oai-did',value,domain=domain,rest={}))
            jar.set_cookie(create_cookie('oai-locale',value,domain=domain,rest={}))
        jar.set_cookie(create_cookie('visible','value',domain='auth.openai.com',rest={}))
        js=session.auth_document_cookie().split('; ')
        self.assertEqual([p for p in js if p.startswith('oai-did=')],['oai-did=authoritative-device'])
        self.assertEqual([p for p in js if p.startswith('oai-locale=')],['oai-locale=ja-JP'])
        self.assertIn('visible=value',js)
        # JS导出不擅自改写HTTP Jar。
        self.assertEqual(jar.get('oai-did',domain='.openai.com'),'old-parent')

    def test_host_only_parent_cookie_is_not_visible_to_subdomain(self):
        session=self.session()
        cookie=create_cookie('parent-host-only','secret',domain='openai.com',rest={})
        cookie.domain_specified=False
        session.session.cookies.jar.set_cookie(cookie)
        self.assertNotIn('parent-host-only',session.auth_document_cookie())
        self.assertNotIn('parent-host-only',session.auth_cookie_header())

    def test_overflowed_cache_timestamp_is_treated_as_corrupt_manifest(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(cache,'_CACHE',Path(directory)), \
             patch.object(settings,'SENTINEL_SDK_AUTO_UPDATE',True):
            root=Path(directory)/settings.SENTINEL_SV
            root.mkdir()
            (root/'current.json').write_text(json.dumps({'sha256':'0'*64,'fetched_at':10**400}))
            session=MagicMock()
            session.get.return_value.status_code=200
            session.get.return_value.content=b'var SentinelSDK={};'
            path=cache.resolve_sentinel_sdk(session)
            self.assertEqual(path.read_bytes(),b'var SentinelSDK={};')
            session.get.assert_called_once()

    def test_bundled_sdk_cannot_be_labelled_as_a_different_online_version(self):
        session=MagicMock()
        with patch.object(settings,'SENTINEL_SDK_AUTO_UPDATE',False), \
             patch.object(settings,'SENTINEL_SV','differentversion'):
            with self.assertRaisesRegex(RuntimeError,'内置 Sentinel SDK 版本'):
                cache.resolve_sentinel_sdk(session)
        session.get.assert_not_called()

    def test_wreq_host_only_cookies_are_scoped_by_native_url_lookup(self):
        import wreq
        from core.wreq_session import WreqSession
        client=WreqSession('chrome146')
        self.addCleanup(client.close)
        client.cookies.native.add(wreq.Cookie('same','auth',path='/'),'https://auth.openai.com/')
        client.cookies.native.add(wreq.Cookie('same','chatgpt',path='/'),'https://chatgpt.com/')
        client.cookies.native.add(wreq.Cookie('chat-only','hidden',path='/'),'https://chatgpt.com/')
        session=self.session()
        session.session=client
        cookie=session.auth_document_cookie()
        self.assertIn('same=auth',cookie)
        self.assertNotIn('same=chatgpt',cookie)
        self.assertNotIn('chat-only',cookie)
        auth_cookie=client.cookies.jar_for_url('https://auth.openai.com/').get('same')
        self.assertEqual(auth_cookie,'auth')

    def test_wreq_server_host_only_device_id_can_be_synchronized(self):
        import wreq
        from core.wreq_session import WreqSession
        client=WreqSession('chrome146')
        self.addCleanup(client.close)
        client.cookies.native.add(wreq.Cookie('oai-did','server-device',path='/'),'https://auth.openai.com/')
        session=self.session()
        session.session=client
        self.assertEqual(session.sync_device_id_from_cookies('auth.openai.com'),'server-device')
        self.assertIn('oai-did=server-device',session.auth_document_cookie())

    def test_wreq_scoped_snapshot_handles_parent_domain_and_cookie_paths(self):
        import wreq
        from core.wreq_session import WreqSession
        client=WreqSession('chrome146')
        self.addCleanup(client.close)
        client.cookies.native.add(wreq.Cookie('parent','value',domain='openai.com',path='/'),
                                  'https://openai.com/')
        client.cookies.native.add(wreq.Cookie('api','value',path='/api'),
                                  'https://auth.openai.com/api/request')
        client.cookies.native.add(wreq.Cookie('implicit','value'),
                                  'https://auth.openai.com/api/request')
        root=client.cookies.jar_for_url('https://auth.openai.com/')
        nested=client.cookies.jar_for_url('https://auth.openai.com/api/next')
        other=client.cookies.jar_for_url('https://auth.openai.com/api2')
        self.assertEqual(root.get('parent'),'value')
        self.assertIsNone(root.get('api'))
        self.assertIsNone(root.get('implicit'))
        self.assertEqual(nested.get('parent'),'value')
        self.assertEqual(nested.get('api'),'value')
        self.assertEqual(nested.get('implicit'),'value')
        self.assertIsNone(other.get('api'))
        self.assertIsNone(other.get('implicit'))

    def test_wreq_workspace_cookie_selection_is_scoped_for_registration_and_codex(self):
        import base64
        import wreq
        from core.wreq_session import WreqSession
        from core import protocol_registration, codex_oauth
        client=WreqSession('chrome146')
        self.addCleanup(client.close)
        for host,workspace in (('auth.openai.com','auth-workspace'),('chatgpt.com','wrong-workspace')):
            value=base64.urlsafe_b64encode(json.dumps({'workspaces':[{'id':workspace}]}).encode()).decode().rstrip('=')
            client.cookies.native.add(wreq.Cookie('oai-client-auth-session',value,path='/'),f'https://{host}/')
        session=self.session()
        session.session=client
        self.assertEqual(protocol_registration.workspace_id(session),'auth-workspace')
        self.assertEqual(codex_oauth._get_workspace_id(session),'auth-workspace')

    def test_sixteen_node_workers_keep_cookie_locale_and_environment_isolated(self):
        script=FAKE_SDK.replace("return 'offline-requirements';",'''return JSON.stringify({
          cookie:document.cookie,language:navigator.language,
          zone:Intl.DateTimeFormat().resolvedOptions().timeZone});''')
        before=os.environ.get('TZ')
        with tempfile.TemporaryDirectory() as directory:
            sdk=Path(directory)/'sdk.js'
            sdk.write_text(script)
            def one(index):
                zone='Asia/Tokyo' if index%2 else 'UTC'
                lang='ja-JP' if index%2 else 'en-US'
                cookie=f'oai-did=device-{index}; fixture={index}'
                proof=runner.generate_sentinel_requirements(flow='offline',device_id=f'device-{index}',
                    sentinel_sid=f'sid-{index}',sdk_path=sdk,cookie=cookie,
                    browser_profile={'timezone_iana':zone,'navigator_language':lang,'navigator_languages':[lang]})
                self.assertEqual(json.loads(proof),{'cookie':cookie,'language':lang,'zone':zone})
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(one,range(16)))
        self.assertEqual(os.environ.get('TZ'),before)
