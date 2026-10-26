"""SDK 初始 proof、缓存及 SO 采集的纯离线回归。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from core import sentinel_sdk as cache, sentinel_runner as runner, openai_auth
from config import openai_protocol as settings


class SdkCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = patch.object(cache, '_CACHE', self.root)
        p.start()
        self.addCleanup(p.stop)
        p = patch.multiple(settings, SENTINEL_SDK_AUTO_UPDATE=True, SENTINEL_SDK_REFRESH_SECONDS=86400)
        p.start()
        self.addCleanup(p.stop)
        self.s = MagicMock()
        self.s.get.return_value.status_code = 200
        self.s.get.return_value.content = b'var SentinelSDK={init(){}};'

    def test_cache_reuse_and_pinned_session_do_not_repeat_download(self):
        first = cache.resolve_sentinel_sdk(self.s)
        self.assertEqual(first.read_bytes(), self.s.get.return_value.content)
        self.assertEqual(cache.resolve_sentinel_sdk(self.s), first)
        other = MagicMock()
        self.assertEqual(cache.resolve_sentinel_sdk(other), first)
        self.s.get.assert_called_once()
        other.get.assert_not_called()

    def test_refresh_keeps_old_session_on_immutable_script(self):
        first = cache.resolve_sentinel_sdk(self.s)
        manifest = first.parent / 'current.json'
        data = json.loads(manifest.read_text())
        data['fetched_at'] = 0
        manifest.write_text(json.dumps(data))
        other = MagicMock()
        other.get.return_value.status_code = 200
        other.get.return_value.content = b'var SentinelSDK={newVersion:true};'
        second = cache.resolve_sentinel_sdk(other)
        self.assertNotEqual(first, second)
        self.assertEqual(cache.resolve_sentinel_sdk(self.s), first)
        self.assertEqual(first.read_bytes(), b'var SentinelSDK={init(){}};')

    def test_bad_download_never_replaces_valid_cache(self):
        for content in (b'', b'<html>SentinelSDK</html>', b'not javascript'):
            self.s.get.return_value.content = content
            with self.assertRaises(RuntimeError):
                cache.resolve_sentinel_sdk(self.s)
        self.assertFalse(any(self.root.rglob('current.json')))

    def test_network_error_propagates_without_silent_bundled_fallback(self):
        self.s.get.side_effect = TimeoutError('network')
        with self.assertRaises(TimeoutError):
            cache.resolve_sentinel_sdk(self.s)

    def test_explicit_bundled_mode_does_not_download(self):
        with patch.object(settings, 'SENTINEL_SDK_AUTO_UPDATE', False):
            self.assertEqual(cache.resolve_sentinel_sdk(self.s), cache._BUNDLED)
        self.s.get.assert_not_called()

    def test_lost_pinned_script_fails_instead_of_changing_version(self):
        self.s._sentinel_sdk_path = self.root / 'missing.js'
        with self.assertRaises(FileNotFoundError):
            cache.resolve_sentinel_sdk(self.s)
        self.s.get.assert_not_called()

    def test_corrupted_cached_script_is_not_used(self):
        first = cache.resolve_sentinel_sdk(self.s)
        first.write_bytes(b'corrupted')
        other = MagicMock()
        other.get.side_effect = TimeoutError('offline')
        with self.assertRaises(TimeoutError):
            cache.resolve_sentinel_sdk(other)
        other.get.assert_called_once()

    def test_concurrent_new_sessions_share_one_download(self):
        sessions = [MagicMock() for _ in range(4)]
        for s in sessions:
            s.get.return_value.status_code = 200
            s.get.return_value.content = b'var SentinelSDK={};'
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(cache.resolve_sentinel_sdk, sessions))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(sum(s.get.call_count for s in sessions), 1)


class SdkPipelineTests(unittest.TestCase):
    def test_real_bundled_sdk_generates_requirements_without_network(self):
        proof = runner.generate_sentinel_requirements(flow='username_password_create',
            device_id='offline-device', sentinel_sid='offline-sid', browser_profile={}, sdk_path=runner._SDK_PATH)
        self.assertIsInstance(proof, str)
        self.assertTrue(proof.startswith('gAAAAA'))

    def test_target_hooked_sdk_solve_binds_proof_and_collects_behavior(self):
        # 最小 SDK 夹具保留目标三处 hook 锚点，并通过实际 iframe bridge 交换本地 challenge。
        script = r'''var SentinelSDK=function(t){
          var events=[];
          class O { async getRequirementsToken(){return 'fallback-proof';}
                    async getEnforcementToken(){return 'fallback-final';} }
          var E=new O;
          function Rn(){return 'turnstile';} function D(){}
          async function je(flow){
            for(const type of ['pointermove','click','scroll','wheel','keydown'])
              document.addEventListener(type,()=>events.push(type));
            const iframe=document.createElement('iframe');
            const result=await new Promise(resolve=>{
              window.addEventListener('message',e=>resolve(e.data.result));
              iframe.contentWindow.postMessage({type:'token',p:'fake-proof',requestId:'r'},'https://sentinel.openai.com');
            });
            return JSON.stringify({p:result.cachedProof,c:result.cachedChatReq.token,id:'offline-device',flow});
          }
          t.init=async function(){return 'init';};
          t.sessionObserverToken=async function(){return events.length?events.join(','):'';};
          return t,t.token=je,t}({});'''
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / 'sdk.js'
            sdk.write_text(script)
            for duration in (0, 1):
                with patch.object(settings, 'SENTINEL_SO_COLLECTION_MS', duration):
                    value = json.loads(runner.generate_sentinel_token(
                        {'token':'challenge','_request_p':'bound-proof'},
                        'email_otp_validate','offline-device',sdk_path=sdk))
                self.assertEqual(value['p'], 'bound-proof')
                if duration:
                    for event in ('pointermove','click','scroll','wheel','keydown'):
                        self.assertIn(event, value['_so'])
                else:
                    self.assertNotIn('_so', value)

    def test_hook_mismatch_fails_explicitly_without_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / 'sdk.js'
            sdk.write_text('var SentinelSDK={};')
            with self.assertRaisesRegex(RuntimeError, '退出码 3.*MISS'):
                runner.generate_sentinel_requirements(flow='x', device_id='d', sentinel_sid='s',
                    browser_profile={}, sdk_path=sdk)

    def test_requirements_output_validation_rejects_empty_or_nonobject(self):
        for stdout in ('[]', '{}', '{"request_p":12}', '{"request_p":""}', '{"request_p":" "}'):
            result = subprocess.CompletedProcess([], 0, stdout, '')
            with patch.object(runner, '_run_node', return_value=result):
                with self.assertRaises(RuntimeError):
                    runner.generate_sentinel_requirements(flow='x', device_id='d', sentinel_sid='s',
                        browser_profile={}, sdk_path=runner._SDK_PATH)

    def test_mismatched_device_or_flow_is_rejected(self):
        for field in ('id', 'flow', 'p', 'c'):
            data = {'p':'proof', 'c':'challenge', 'id':'device', 'flow':'flow'}
            data[field] = '' if field in ('p', 'c') else 'wrong'
            result = subprocess.CompletedProcess([], 0, json.dumps({'token':json.dumps(data)}), '')
            with patch.object(runner, '_run_node', return_value=result):
                with self.assertRaises(RuntimeError):
                    runner.generate_sentinel_token({'token':'challenge'}, 'flow', 'device')

    def test_required_so_missing_is_not_submitted_as_success(self):
        s = MagicMock()
        s.device_id = 'd'
        s.browser_profile = {}
        with patch.object(openai_auth, 'generate_sentinel_token', return_value='{"p":"p","c":"c","id":"d","flow":"x"}'):
            with self.assertRaisesRegex(RuntimeError, '未返回 SO'):
                openai_auth.build_sentinel_header(s, {'so':{'required':True}}, 'x')

    def test_solve_uses_exact_sdk_pinned_during_requirements(self):
        s = MagicMock()
        s.device_id = 'd'
        s.browser_profile = {}
        s._sentinel_sdk_path = Path('/tmp/offline-pinned-sdk.js')
        with patch.object(openai_auth, 'generate_sentinel_token', return_value='{"p":"p","c":"c","id":"d","flow":"x"}') as generate:
            openai_auth.build_sentinel_header(s, {}, 'x')
        self.assertEqual(generate.call_args.kwargs['sdk_path'], s._sentinel_sdk_path)
