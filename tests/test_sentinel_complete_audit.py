"""注册完整 Sentinel 链路离线审计：原生 SDK 输出形状、停止/超时、Cookie隔离。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from requests.cookies import RequestsCookieJar, create_cookie
from config import openai_protocol as settings
from core import openai_auth as auth, sentinel_runner as runner
from core.registration_service import StopRequested
from core.session import BrowserSession
from tests.test_sentinel_target_executor import FAKE_SDK
from tests.test_openai_auth_registration_flow import _ProtocolSession


class SentinelOutputContractTests(unittest.TestCase):
    def test_full_so_json_is_not_double_encoded(self):
        data = {'so':'observer', 'c':'challenge', 'id':'device', 'flow':'flow'}
        actual = auth._format_sentinel_so_header(json.dumps(data), challenge='challenge', device_id='device', flow='flow')
        self.assertEqual(json.loads(actual), data)
        self.assertEqual(json.loads(actual)['so'], 'observer')

    def test_so_context_mismatch_is_rejected(self):
        for field in ('c','id','flow'):
            data = {'so':'observer', 'c':'challenge', 'id':'device', 'flow':'flow'}
            data[field] = 'wrong'
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError,'不匹配'):
                auth._format_sentinel_so_header(json.dumps(data), challenge='challenge', device_id='device', flow='flow')

    def test_invalid_structured_so_is_rejected(self):
        for value in ('{bad', '{}', '{"so":""}', '{"so":123}', '{"so":"o","e":"failure"}'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                auth._format_sentinel_so_header(value, challenge='c', device_id='d', flow='f')

    def test_legacy_raw_so_remains_supported(self):
        self.assertEqual(json.loads(auth._format_sentinel_so_header('observer', challenge='c', device_id='d', flow='f')),
                         {'so':'observer','c':'c','id':'d','flow':'f'})

    def test_sdk_error_token_cannot_be_accepted_as_success(self):
        for key in ('e','error'):
            token = {'p':'proof', 'c':'c', 'id':'d', 'flow':'f', key:''}
            with patch.object(runner, '_run_node', return_value=subprocess.CompletedProcess([],0,json.dumps({'token':token}),'')):
                with self.assertRaisesRegex(RuntimeError, '错误 token'):
                    runner.generate_sentinel_token({'token':'c'},'f','d')

    def test_required_turnstile_cannot_be_missing(self):
        for field in ({'required':True}, {'dx':'local-fixture'}):
            result = {'token':{'p':'proof','c':'c','id':'d','flow':'f'}}
            with patch.object(runner, '_run_node', return_value=subprocess.CompletedProcess([],0,json.dumps(result),'')):
                with self.assertRaisesRegex(RuntimeError, 'turnstile token'):
                    runner.generate_sentinel_token({'token':'c','turnstile':field},'f','d')

    def test_challenge_business_failure_is_not_accepted(self):
        for data in ({'token':'c','success':False}, {'token':'c','error':'bad'}, {'token':'c','e':'bad'}):
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, '业务失败'):
                auth._validate_sentinel_response(data, 'proof')


class NodeLifecycleTests(unittest.TestCase):
    def run_child(self, code, *, timeout=2, input='fixture'):
        return runner._run_node([sys.executable, '-c', code], input=input,
                                cwd=str(runner._PROJECT_ROOT), timeout=timeout, env=os.environ.copy())

    def test_communicate_retries_do_not_duplicate_input_and_drain_both_pipes(self):
        code = "import sys,time; data=sys.stdin.read(); time.sleep(.35); sys.stderr.write('x'*200000); print(len(data))"
        with patch.object(auth, '_check_stop_requested'):
            result = self.run_child(code,input='x'*300000)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '300000')
        self.assertEqual(len(result.stderr), 200000)

    def test_stop_before_spawn_does_not_create_child(self):
        with patch.object(auth, '_check_stop_requested', side_effect=StopRequested('stop')), \
             patch.object(runner.subprocess, 'Popen') as spawn:
            with self.assertRaises(StopRequested):
                self.run_child('pass')
        spawn.assert_not_called()

    def test_stop_kills_and_reaps_running_child(self):
        processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        started = time.monotonic()
        def check():
            if time.monotonic()-started >= .12:
                raise StopRequested('stop')
        with patch.object(auth, '_check_stop_requested', side_effect=check), \
             patch.object(runner.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(StopRequested):
                self.run_child('import time; time.sleep(60)',timeout=60)
        self.assertLess(time.monotonic()-started, 2)
        self.assertEqual(len(processes),1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_timeout_kills_and_reaps_child(self):
        processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(auth, '_check_stop_requested'), \
             patch.object(runner.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_child('import time; time.sleep(60)',timeout=.15)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)


class SentinelCookieTests(unittest.TestCase):
    def test_js_cookie_excludes_http_only_children_other_paths_and_expired(self):
        session = object.__new__(BrowserSession)
        session.device_id='device'
        session.browser_profile={'navigator_language':'en-US'}
        class Raw:
            cookies = type('Cookies', (), {'jar':RequestsCookieJar()})()
        session.session=Raw()
        for name, kwargs in (
            ('visible',{'domain':'auth.openai.com'}),
            ('parent',{'domain':'.openai.com'}),
            ('http',{'domain':'auth.openai.com','rest':{'HttpOnly':None}}),
            ('child',{'domain':'child.auth.openai.com'}),
            ('path',{'domain':'auth.openai.com','path':'/api'}),
            ('expired',{'domain':'auth.openai.com','expires':1}),
        ):
            session.session.cookies.jar.set_cookie(create_cookie(name,'value',**{'rest':{},**kwargs}))
        cookie=session.auth_document_cookie()
        for name in ('visible','parent','oai-did','oai-locale'):
            self.assertIn(name+'=',cookie)
        for name in ('http','child','path','expired'):
            self.assertNotIn(name+'=',cookie)
        self.assertIn('http=value',session.auth_cookie_header())

    def test_actual_wreq_snapshot_preserves_http_only_attribute(self):
        from core.wreq_session import WreqSession
        client=WreqSession('chrome146')
        self.addCleanup(client.close)
        client.cookies.set('secret','hidden',domain='auth.openai.com',http_only=True)
        client.cookies.set('visible','value',domain='auth.openai.com')
        cookies={c.name:c for c in client.cookies.jar}
        self.assertTrue(cookies['secret'].has_nonstandard_attr('HttpOnly'))
        self.assertFalse(cookies['visible'].has_nonstandard_attr('HttpOnly'))


class CompleteSentinelPipelineTests(unittest.TestCase):
    def test_each_flow_binds_original_proof_and_preserves_full_so_header(self):
        script=FAKE_SDK.replace("async function je(){throw new Error('exercise fallback');}", '''
          let current;
          async function je(flow){
            const frame=document.createElement('iframe');
            current=await new Promise(resolve=>{
              window.addEventListener('message',event=>resolve(event.data.result));
              frame.contentWindow.postMessage({p:'resampled-proof',requestId:'r'},'https://sentinel.openai.com');
            });
            return JSON.stringify({p:current.cachedProof,c:current.cachedChatReq.token,id:'device-id',flow});
          }
          t.sessionObserverToken=async function(flow){
            return JSON.stringify({so:'observer-'+flow,c:current.cachedChatReq.token,id:'device-id',flow});
          };''')
        with tempfile.TemporaryDirectory() as directory, patch.object(settings,'SENTINEL_SO_COLLECTION_MS',0):
            sdk=Path(directory)/'sdk.js'
            sdk.write_text(script)
            session=_ProtocolSession()
            session._sentinel_sdk_path=sdk
            cookies=[]
            session.auth_document_cookie=lambda:'oai-did=device-id; visible=value'
            with patch.object(auth,'resolve_sentinel_sdk',return_value=sdk), \
                 patch.object(auth,'generate_sentinel_requirements',wraps=runner.generate_sentinel_requirements) as generate:
                for flow in ('authorize_continue','username_password_create','email_otp_validate','oauth_create_account','password_verify'):
                    challenge=auth.request_sentinel_token(session,flow)
                    header,so=auth.build_sentinel_header(session,challenge,flow)
                    self.assertEqual(json.loads(header)['p'],'offline-requirements')
                    self.assertEqual(json.loads(so),{'so':'observer-'+flow,'c':'challenge','id':'device-id','flow':flow})
                    self.assertNotIn('_so',json.loads(header))
                    cookies.append(generate.call_args.kwargs['cookie'])
            self.assertEqual(cookies,['oai-did=device-id; visible=value']*5)
            self.assertEqual(len(session.posts),5)

class SentinelVersionPinTests(unittest.TestCase):
    def test_download_and_cache_hit_pin_version_despite_later_config_changes(self):
        from core import sentinel_sdk as cache
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory, patch.object(cache,'_CACHE',Path(directory)), \
             patch.object(settings,'SENTINEL_SDK_AUTO_UPDATE',True):
            version=settings.SENTINEL_SV
            session=MagicMock()
            session.get.return_value.status_code=200
            session.get.return_value.content=b'var SentinelSDK={};'
            first=cache.resolve_sentinel_sdk(session)
            other=MagicMock()
            self.assertEqual(cache.resolve_sentinel_sdk(other),first)
            self.assertEqual(other._sentinel_sdk_version,version)
            with patch.object(settings,'SENTINEL_SV','newversion'):
                self.assertEqual(cache.resolve_sentinel_sdk(session),first)
                self.assertEqual(cache.sentinel_sdk_version(session),version)
                browser=object.__new__(BrowserSession)
                browser._sentinel_sdk_version=version
                with patch.object(browser,'_get_common_headers',return_value={}):
                    self.assertIn('sv='+version,browser.get_sentinel_headers()['referer'])
            other.get.assert_not_called()

    def test_frame_navigation_uses_pinned_version_for_normal_and_password_bundle(self):
        session=_ProtocolSession()
        session._sentinel_sdk_version='pinnedversion'
        with patch.object(auth,'resolve_sentinel_sdk',return_value=runner._SDK_PATH), \
             patch.object(auth,'generate_sentinel_requirements',return_value='proof'):
            auth.request_sentinel_token(session,'email_otp_validate')
            auth.request_password_sentinel_bundle(session)
        self.assertEqual(len(session.gets),2)
        for url,_,_ in session.gets:
            self.assertIn('sv=pinnedversion',url)
