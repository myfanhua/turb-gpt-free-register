"""移植执行器的本地 hook、回退、错误边界、画像和 SO 传递。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from config import openai_protocol as settings
from core import sentinel_runner as runner

# 精确保留目标 hook 锚点，但所有 proof/challenge 均为固定离线夹具。
FAKE_SDK = '''var SentinelSDK=function(t){
class O {
  async getRequirementsToken(){return 'offline-requirements';}
  async getEnforcementToken(){return 'offline-enforcement';}
}
var E=new O;
function D(challenge, proof){globalThis.boundProof=proof;}
async function Rn(){return globalThis.boundProof;}
async function je(){throw new Error('exercise fallback');}
t.init=async function(){throw new Error('exercise requirements fallback');};
return t,t.token=je,t}({});'''


class TargetExecutorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sdk = Path(tmp.name) / 'sdk.js'
        self.sdk.write_text(FAKE_SDK)

    def test_requirements_uses_target_internal_fallback(self):
        proof = runner.generate_sentinel_requirements(flow='flow', device_id='device',
            sentinel_sid='sid', browser_profile={}, sdk_path=self.sdk)
        self.assertEqual(proof, 'offline-requirements')

    def test_solve_fallback_outputs_complete_token_and_binds_original_proof(self):
        token = json.loads(runner.generate_sentinel_token(
            {'token':'challenge', '_request_p':'original-proof', 'turnstile':{'dx':'offline'}},
            'flow', 'device', sdk_path=self.sdk))
        self.assertEqual(token, {'p':'offline-enforcement', 't':'original-proof',
                                'c':'challenge', 'id':'device', 'flow':'flow'})

    def test_fallback_without_original_proof_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'missing request_p'):
            runner.generate_sentinel_token({'token':'challenge'}, 'flow', 'device', sdk_path=self.sdk)

    def test_fallback_cannot_satisfy_required_so(self):
        with self.assertRaisesRegex(RuntimeError, '未返回 SO'):
            runner.generate_sentinel_token({'token':'challenge', '_request_p':'proof', 'so':{'required':True}},
                                           'flow', 'device', sdk_path=self.sdk)

    def test_post_load_hook_check_fails_even_when_strings_match(self):
        self.sdk.write_text(FAKE_SDK.replace('function D(challenge, proof)', 'function D_impl(challenge, proof)')
                            .replace('function D_impl', 'var D=undefined; function D_impl'))
        with self.assertRaisesRegex(RuntimeError, 'POST-LOAD CHECK FAILED'):
            runner.generate_sentinel_requirements(flow='flow', device_id='device',
                sentinel_sid='sid', browser_profile={}, sdk_path=self.sdk)

    def test_token_envelope_and_challenge_validation(self):
        token = {'p':'p', 'c':'challenge', 'id':'device', 'flow':'flow'}
        for result in ({}, {'token':'[]'}, {'token':'bad'},
                       {'token':{**token,'c':'wrong'}}, {'token':token,'so_token':123}):
            with self.subTest(result=result), patch.object(runner, '_run_node',
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(result), '')):
                with self.assertRaises(RuntimeError):
                    runner.generate_sentinel_token({'token':'challenge'}, 'flow', 'device')

    def test_timeout_and_missing_node_have_clear_errors(self):
        for error, message in ((subprocess.TimeoutExpired('node', 60), '执行超时'),
                               (FileNotFoundError(), '未找到 Node')):
            with patch.object(runner, '_run_node', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, message):
                    runner.generate_sentinel_token({}, 'flow', 'device')

    def test_cookie_sid_timezone_and_device_profile_are_visible_to_sdk(self):
        script = FAKE_SDK.replace("return 'offline-requirements';", '''return JSON.stringify({
          cookie:document.cookie, sid:crypto.randomUUID(), timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
          width:screen.width, language:navigator.language, vendor:navigator.vendor,
          memory:navigator.deviceMemory, ratio:devicePixelRatio});''')
        self.sdk.write_text(script)
        proof = runner.generate_sentinel_token({}, 'flow', 'device',
            browser_profile={'timezone_iana':'Asia/Tokyo', 'screen_width':1680,
                             'navigator_language':'ja-JP', 'navigator_vendor':'',
                             'device_memory':8, 'device_pixel_ratio':2},
            cookie='oai-did=device; fixture=value', sentinel_sid='fixed-sid',
            sdk_path=self.sdk, action='requirements')
        values = json.loads(proof)
        self.assertEqual(values, {'cookie':'oai-did=device; fixture=value', 'sid':'fixed-sid',
                                 'timezone':'Asia/Tokyo', 'width':1680, 'language':'ja-JP',
                                 'vendor':'', 'memory':8, 'ratio':2})

    def test_public_sdk_success_cleans_long_running_vm_timers(self):
        script = FAKE_SDK.replace("throw new Error('exercise requirements fallback');",
            "setInterval(()=>{},60000); setTimeout(()=>{},60000); return 'ok';")
        self.sdk.write_text(script)
        result = subprocess.run(['node', str(runner._RUNNER_PATH)],
            input=json.dumps({'action':'requirements'}), text=True, capture_output=True,
            env={**__import__('os').environ, 'OPENAI_SENTINEL_SDK_FILE':str(self.sdk)}, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['request_p'], 'offline-requirements')
