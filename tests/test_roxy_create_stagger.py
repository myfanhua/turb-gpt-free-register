# -*- coding: utf-8 -*-
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from core import roxybrowser_client as roxy


class RoxyCreateStaggerTests(unittest.TestCase):
    def setUp(self):
        roxy._ROXY_CREATE_LAST_FINISHED_AT = 0.0

    def tearDown(self):
        roxy._ROXY_CREATE_LAST_FINISHED_AT = 0.0

    @staticmethod
    def _client():
        return roxy.RoxyBrowserClient(api_base="http://127.0.0.1:1", token="")

    def test_create_requests_are_serialized(self):
        active = 0
        max_active = 0
        guard = threading.Lock()
        sequence = iter(("profile-1", "profile-2"))

        def fake_request(*_args, **_kwargs):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                profile_id = next(sequence)
            time.sleep(0.03)
            with guard:
                active -= 1
            return {"data": {"id": profile_id}}

        with (
            patch.object(roxy._cfg, "ROXY_WORKSPACE_ID", "1"),
            patch.object(roxy._cfg, "ROXY_PROJECT_ID", "2"),
            patch.object(roxy._cfg, "ROXY_CREATE_STAGGER_DELAY", 0.0, create=True),
            patch.object(roxy.RoxyBrowserClient, "request", side_effect=fake_request),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: self._client().create_profile(), range(2)))

        self.assertEqual(set(results), {"profile-1", "profile-2"})
        self.assertEqual(max_active, 1)

    def test_create_requests_wait_for_configured_gap(self):
        client = self._client()
        with (
            patch.object(roxy._cfg, "ROXY_WORKSPACE_ID", "1"),
            patch.object(roxy._cfg, "ROXY_PROJECT_ID", "2"),
            patch.object(roxy._cfg, "ROXY_CREATE_STAGGER_DELAY", 3.0, create=True),
            patch.object(roxy.RoxyBrowserClient, "request", side_effect=[
                {"data": {"id": "profile-1"}},
                {"data": {"id": "profile-2"}},
            ]),
            patch.object(roxy.time, "monotonic", side_effect=[100.0, 101.0, 102.0, 103.0]),
            patch.object(roxy.time, "sleep") as sleep,
        ):
            self.assertEqual(client.create_profile(), "profile-1")
            self.assertEqual(client.create_profile(), "profile-2")

        sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
