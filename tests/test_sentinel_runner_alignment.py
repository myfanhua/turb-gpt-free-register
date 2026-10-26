# -*- coding: utf-8 -*-
import json
import subprocess
import unittest
from unittest.mock import patch

from core import sentinel_runner as runner


class SentinelRunnerAlignmentTests(unittest.TestCase):
    def test_target_executor_receives_session_profile_and_proof_over_stdin(self):
        token = {'p': 'proof', 'c': 'challenge', 'id': 'device', 'flow': 'flow'}
        completed = subprocess.CompletedProcess([], 0, json.dumps({'token': json.dumps(token), 'so_token': 'so'}), '')
        profile = {'screen_width': 1680, 'screen_height': 1050,
                   'hardware_concurrency': 4, 'navigator_platform': 'MacIntel',
                   'navigator_vendor': '', 'timezone_iana': 'Asia/Tokyo',
                   'navigator_language': 'ja-JP', 'navigator_languages': ['ja-JP', 'en'],
                   'device_memory': 8, 'device_pixel_ratio': 2}
        with patch.object(runner, '_run_node', return_value=completed) as run:
            output = json.loads(runner.generate_sentinel_token(
                {'token': 'challenge', '_request_p': 'bound-proof', 'so': {'required': True}},
                'flow', 'device', browser_profile=profile, sentinel_sid='sid', cookie='oai-did=device; test=x'))
        self.assertEqual(output['_so'], 'so')
        self.assertEqual(len(run.call_args.args[0]), 2)
        self.assertTrue(run.call_args.args[0][1].endswith('openai_sentinel_quickjs.js'))
        body = json.loads(run.call_args.kwargs['input'])
        for key, value in {'screen_width':1680, 'screen_height':1050, 'hardware_concurrency':4,
                           'platform':'MacIntel', 'vendor':'', 'timezone':'Asia/Tokyo',
                           'language':'ja-JP', 'languages':['ja-JP', 'en'], 'sentinel_sid':'sid',
                           'request_p':'bound-proof', 'action':'solve', 'cookie':'oai-did=device; test=x'}.items():
            self.assertEqual(body[key], value)
        self.assertNotIn('_request_p', body['challenge'])
        self.assertEqual(run.call_args.kwargs['env']['TZ'], 'Asia/Tokyo')
        self.assertEqual(run.call_args.kwargs['env']['OPENAI_SENTINEL_SDK_FILE'], str(runner._SDK_PATH))

    def test_empty_vendor_is_not_replaced_for_firefox(self):
        result = subprocess.CompletedProcess([], 0, '{"request_p":"proof"}', '')
        with patch.object(runner, '_run_node', return_value=result) as run:
            runner.generate_sentinel_requirements(flow='flow', device_id='device', sentinel_sid='sid',
                browser_profile={'navigator_vendor':'', 'user_agent':'Firefox/140'}, sdk_path=runner._SDK_PATH)
        body = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(body['vendor'], '')
        self.assertEqual(body['action'], 'requirements')


if __name__ == '__main__':
    unittest.main()
