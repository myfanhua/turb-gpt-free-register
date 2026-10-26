"""移植后审计：坏响应、协议输出污染、缓存锁竞争和动画 timer 取消。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core import openai_auth, sentinel_runner as runner, sentinel_sdk as cache
from tests.test_sentinel_target_executor import FAKE_SDK


class SentinelPostAlignmentAuditTests(unittest.TestCase):
    def test_challenge_rejects_nonobjects_missing_token_and_bad_capabilities(self):
        for data in ([], None, 'bad', {}, {'token':''}, {'token':123},
                     {'token':'c','so':[]}, {'token':'c','turnstile':True},
                     {'token':'c','proofofwork':'bad'}):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                openai_auth._validate_sentinel_response(data, 'proof')

    def test_challenge_normalizes_null_capabilities_without_mutating_response(self):
        data = {'token':'c', 'so':None, 'turnstile':None, 'proofofwork':None, '_request_p':'wrong'}
        self.assertEqual(openai_auth._validate_sentinel_response(data, 'proof'),
                         {'token':'c', '_request_p':'proof'})
        self.assertEqual(data['so'], None)
        self.assertEqual(data['_request_p'], 'wrong')

    def test_so_rejects_falsey_nonstring_and_whitespace_values(self):
        for so in (False, 0, [], {}, ' ', '\n'):
            result = {'token':{'p':'p','c':'c','id':'d','flow':'f'}, 'so_token':so}
            with self.subTest(so=so), patch.object(runner, '_run_node',
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(result), '')):
                with self.assertRaises(RuntimeError):
                    runner.generate_sentinel_token({'token':'c'}, 'f', 'd')

    def test_sdk_console_output_does_not_corrupt_protocol_json(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / 'sdk.js'
            sdk.write_text(FAKE_SDK.replace("return 'offline-requirements';",
                "console.log('fixture-log'); console.warn('fixture-warning'); return 'offline-requirements';"))
            self.assertEqual(runner.generate_sentinel_requirements(flow='f',device_id='d',
                sentinel_sid='sid',browser_profile={},sdk_path=sdk), 'offline-requirements')

    def test_request_animation_frame_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / 'sdk.js'
            sdk.write_text(FAKE_SDK.replace("return 'offline-requirements';", '''
                let called=false;
                const frame=requestAnimationFrame(()=>{called=true;});
                cancelAnimationFrame(frame);
                await new Promise(resolve=>setTimeout(resolve,40));
                return called?'not-cancelled':'cancelled';'''))
            self.assertEqual(runner.generate_sentinel_requirements(flow='f',device_id='d',
                sentinel_sid='sid',browser_profile={},sdk_path=sdk), 'cancelled')

    def test_cache_wait_is_interruptible_and_does_not_release_foreign_lock(self):
        lock = MagicMock()
        lock.acquire.return_value = False
        with patch.object(cache, '_LOCK', lock), \
             patch.object(openai_auth, '_check_stop_requested', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                with cache._cache_lock():
                    self.fail('must not enter')
        lock.acquire.assert_called_once_with(timeout=0.25)
        lock.release.assert_not_called()

    def test_cache_releases_acquired_lock_if_stop_is_requested(self):
        lock = MagicMock()
        lock.acquire.return_value = True
        with patch.object(cache, '_LOCK', lock), \
             patch.object(openai_auth, '_check_stop_requested', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                with cache._cache_lock():
                    self.fail('must not enter')
        lock.release.assert_called_once()

    def test_nonobject_cache_manifest_is_replaced_by_valid_download(self):
        from config import openai_protocol as settings
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cache, '_CACHE', Path(directory)), \
             patch.object(settings, 'SENTINEL_SDK_AUTO_UPDATE', True):
            root = Path(directory) / settings.SENTINEL_SV
            root.mkdir()
            (root / 'current.json').write_text('null')
            session = MagicMock()
            session.get.return_value.status_code = 200
            session.get.return_value.content = b'var SentinelSDK={};'
            path = cache.resolve_sentinel_sdk(session)
            self.assertTrue(path.is_file())
            self.assertIsInstance(json.loads((root / 'current.json').read_text()), dict)
