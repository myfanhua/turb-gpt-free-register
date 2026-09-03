# -*- coding: utf-8 -*-
import unittest
import threading
import time
from unittest.mock import patch


class RoxyFingerprintTests(unittest.TestCase):
    def test_open_failure_cleans_new_profile_before_releasing_capacity(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        with patch.object(roxy_cfg, "ROXY_MAX_CONCURRENT_PROFILES", 2, create=True), patch.object(
            roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True
        ), patch.object(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True), patch.object(
            client, "create_profile", return_value="profile-open-failed"
        ), patch.object(client, "request", side_effect=RuntimeError("open failed")), patch.object(
            client, "close_profile"
        ) as close_profile, patch.object(client, "delete_profile") as delete_profile:
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                client.open_profile()

        close_profile.assert_called_once_with("profile-open-failed")
        delete_profile.assert_called_once_with("profile-open-failed")

    def test_open_profiles_respect_roxy_window_capacity(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        clients = [RoxyBrowserClient(api_base="http://127.0.0.1:1") for _ in range(3)]
        state_lock = threading.Lock()
        state = {"active": 0, "max_active": 0}

        for index, client in enumerate(clients, 1):
            client.create_profile = lambda proxy=None, index=index: f"profile-{index}"
            client.request = lambda *args, **kwargs: {"data": {"http": "127.0.0.1:9222"}}
            client.close_profile = lambda profile_id: None
            client.delete_profile = lambda profile_id: None

        def hold_profile(client):
            opened = client.open_profile()
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.08)
            with state_lock:
                state["active"] -= 1
            client.cleanup_profile(opened)

        with patch.object(roxy_cfg, "ROXY_MAX_CONCURRENT_PROFILES", 2, create=True), patch.object(
            roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True
        ), patch.object(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True), patch.object(
            roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False
        ):
            threads = [threading.Thread(target=hold_profile, args=(client,)) for client in clients]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state["max_active"], 2)

    def test_concurrent_profile_creates_are_serialized(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        clients = [
            RoxyBrowserClient(api_base="http://127.0.0.1:1"),
            RoxyBrowserClient(api_base="http://127.0.0.1:1"),
        ]
        state_lock = threading.Lock()
        first_entered = threading.Event()
        state = {"active": 0, "max_active": 0, "next_id": 0}

        def create_request(*args, **kwargs):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["next_id"] += 1
                profile_id = f"profile-{state['next_id']}"
                first_entered.set()
            time.sleep(0.08)
            with state_lock:
                state["active"] -= 1
            return {"data": {"dirId": profile_id}}

        for instance in clients:
            instance.request = create_request

        results = []
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}), patch.object(
            roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False
        ), patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), patch.object(
            roxy_cfg, "ROXY_DEFAULT_OS", "macOS"
        ), patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", False, create=True), patch.object(
            RoxyBrowserClient, "_resolve_workspace_selection", return_value=("workspace-test", "project-test")
        ):
            first = threading.Thread(target=lambda: results.append(clients[0].create_profile()))
            second = threading.Thread(target=lambda: results.append(clients[1].create_profile()))
            first.start()
            self.assertTrue(first_entered.wait(timeout=1))
            second.start()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(state["max_active"], 1)

    def test_create_profile_retries_explicit_roxy_busy_response(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        busy = RuntimeError("Roxy API 返回失败 POST /browser/create: 正在创建中，请稍等！")
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}), patch.object(
            roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False
        ), patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), patch.object(
            roxy_cfg, "ROXY_DEFAULT_OS", "macOS"
        ), patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", False, create=True), patch.object(
            RoxyBrowserClient, "_resolve_workspace_selection", return_value=("workspace-test", "project-test")
        ), patch.object(client, "request", side_effect=[busy, {"data": {"dirId": "profile-retried"}}]) as request, patch(
            "core.roxybrowser_client.time.sleep"
        ) as sleep:
            profile_id = client.create_profile()

        self.assertEqual(profile_id, "profile-retried")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    def test_create_profile_waits_and_retries_when_roxy_window_quota_is_full(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        full = RuntimeError("Roxy API 返回失败 POST /browser/create: 窗口额度不足")
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}), patch.object(
            roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False
        ), patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), patch.object(
            roxy_cfg, "ROXY_DEFAULT_OS", "macOS"
        ), patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", False, create=True), patch.object(
            RoxyBrowserClient, "_resolve_workspace_selection", return_value=("workspace-test", "project-test")
        ), patch.object(client, "request", side_effect=[full, {"data": {"dirId": "profile-after-wait"}}]) as request, patch(
            "core.roxybrowser_client.time.sleep"
        ) as sleep:
            profile_id = client.create_profile()

        self.assertEqual(profile_id, "profile-after-wait")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    def test_create_profile_replaces_stale_workspace_when_one_is_accessible(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        accessible = [{"id": "workspace-current", "projectId": "project-current"}]
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_DEFAULT_OS", "macOS"), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace-stale"), \
                patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project-stale"), \
                patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", False, create=True), \
                patch.object(client, "list_workspaces", return_value={"ok": True, "items": accessible}), \
                patch.object(client, "request", return_value={"data": {"dirId": "profile-current"}}) as request:
            profile_id = client.create_profile()

        self.assertEqual(profile_id, "profile-current")
        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["workspaceId"], "workspace-current")
        self.assertEqual(body["projectId"], "project-current")

    def test_create_profile_requests_roxy_random_fingerprint(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_DEFAULT_OS", "macOS"), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace-test"), \
                patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project-test"), \
                patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", True, create=True), \
                patch.object(client, "request", return_value={"data": {"dirId": "profile-1"}}) as request:
            profile_id = client.create_profile()

        self.assertEqual(profile_id, "profile-1")
        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["fingerInfo"]["randomFingerprint"], True)

    def test_create_profile_preserves_fingerprint_options_when_random_enabled(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        payload = {"fingerInfo": {"webRTC": 1}}
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", payload), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_DEFAULT_OS", "macOS"), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace-test"), \
                patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project-test"), \
                patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", True, create=True), \
                patch.object(roxy_cfg, "ROXY_NATIVE_FINGERPRINT_ONLY", False, create=True), \
                patch.object(roxy_cfg, "ROXY_FINGERPRINT_FOLLOW_PROXY_IP", False, create=True), \
                patch.object(client, "request", return_value={"data": {"dirId": "profile-2"}}) as request:
            client.create_profile()

        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["fingerInfo"], {"webRTC": 1, "randomFingerprint": True})

    def test_native_only_random_fingerprint_discards_partial_manual_overrides(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        configured = {
            "fingerInfo": {
                "webRTC": 1,
                "userAgent": "stale-manual-agent",
                "randomFingerprint": False,
            }
        }
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", configured), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_DEFAULT_OS", "macOS"), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace-test"), \
                patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project-test"), \
                patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", True, create=True), \
                patch.object(roxy_cfg, "ROXY_NATIVE_FINGERPRINT_ONLY", True, create=True), \
                patch.object(roxy_cfg, "ROXY_FINGERPRINT_FOLLOW_PROXY_IP", True, create=True), \
                patch.object(client, "request", return_value={"data": {"dirId": "profile-native"}}) as request:
            client.create_profile()

        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["fingerInfo"], {
            "randomFingerprint": True,
            "isLanguageBaseIp": True,
            "isDisplayLanguageBaseIp": True,
            "isTimeZone": True,
            "isPositionBaseIp": True,
        })

    def test_runtime_profile_invariants_cannot_be_undone_by_call_payload(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        payload = {
            "name": "fixed-stale-name",
            "os": "Linux",
            "osVersion": "stale-version",
            "fingerInfo": {"randomFingerprint": False, "webRTC": 1},
        }
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", True), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", True), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace-test"), \
                patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project-test"), \
                patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", True, create=True), \
                patch.object(roxy_cfg, "ROXY_NATIVE_FINGERPRINT_ONLY", True, create=True), \
                patch.object(roxy_cfg, "ROXY_FINGERPRINT_FOLLOW_PROXY_IP", True, create=True), \
                patch("core.roxybrowser_client._random_roxy_profile_name", return_value="rb-generated"), \
                patch("core.roxybrowser_client._random_roxy_os", return_value="Windows"), \
                patch.object(client, "request", return_value={"data": {"dirId": "profile-invariants"}}) as request:
            client.create_profile(payload=payload)

        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["name"], "rb-generated")
        self.assertEqual(body["os"], "Windows")
        self.assertNotIn("osVersion", body)
        self.assertEqual(body["fingerInfo"], {
            "randomFingerprint": True,
            "isLanguageBaseIp": True,
            "isDisplayLanguageBaseIp": True,
            "isTimeZone": True,
            "isPositionBaseIp": True,
        })

    def test_explicit_task_proxy_overrides_stale_profile_proxy(self):
        from config import roxybrowser as roxy_cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient(api_base="http://127.0.0.1:1")
        stale = {
            "proxyInfo": {
                "proxyMethod": "custom",
                "protocol": "HTTP",
                "host": "stale.example.test",
                "port": "9999",
            }
        }
        with patch.object(roxy_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", stale), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_DEFAULT_OS", "macOS"), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace-test"), \
                patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project-test"), \
                patch.object(roxy_cfg, "ROXY_RANDOM_FINGERPRINT_ON_CREATE", True, create=True), \
                patch.object(roxy_cfg, "ROXY_NATIVE_FINGERPRINT_ONLY", True, create=True), \
                patch.object(client, "request", return_value={"data": {"dirId": "profile-proxy"}}) as request:
            client.create_profile(proxy="http://task-user:task-pass@fresh.example.test:8080")

        proxy_info = request.call_args.kwargs["json_body"]["proxyInfo"]
        self.assertEqual(proxy_info["host"], "fresh.example.test")
        self.assertEqual(proxy_info["port"], "8080")
        self.assertEqual(proxy_info["proxyUserName"], "task-user")


if __name__ == "__main__":
    unittest.main()
