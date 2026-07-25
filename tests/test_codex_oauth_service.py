# -*- coding: utf-8 -*-
"""core.codex_oauth_service 离线单测。"""
import json
import unittest
from unittest import mock

from core import codex_oauth_service as svc


def _acc(acc_id=1, email="a@b.com", proxy="socks5://127.0.0.1:7897", extra=None):
    return {
        "id": acc_id,
        "email": email,
        "proxy_used": proxy,
        "extra_json": json.dumps(extra or {}),
    }


class PreflightTests(unittest.TestCase):
    def test_preflight_shape_and_local_source_ok(self):
        with mock.patch("config.codex.CODEX_AUTH_URL_SOURCE", "local"), \
             mock.patch("config.codex.SMS_PROVIDER", "l"), \
             mock.patch("config.codex.L_API_BASE", "http://localhost:1"), \
             mock.patch("config.codex.L_ADMIN_AUTH_CODE", ""):
            info = svc.preflight()
        self.assertEqual(info["auth_url_source"], "local")
        self.assertEqual(info["sms_provider"], "l")
        self.assertIn("warnings", info)
        self.assertFalse(info["ready"])  # L 服务不可达 + 未配置授权码
        self.assertFalse(any("CODEX_AUTH_URL_SOURCE" in w for w in info["warnings"]))

    def test_preflight_warns_on_cpa_source(self):
        with mock.patch("config.codex.CODEX_AUTH_URL_SOURCE", "cpa"), \
             mock.patch("config.codex.SMS_PROVIDER", "grizzly"), \
             mock.patch("config.codex.SMS_API_KEY", "k"):
            info = svc.preflight()
        self.assertTrue(any("cpa" in w for w in info["warnings"]))


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        svc._ENQUEUED.clear()

    def test_enqueue_rejects_bad_ids(self):
        with mock.patch("core.db.get_account", return_value=None):
            res = svc.enqueue_accounts([999, "x"])
        self.assertEqual(res["queued"], [])
        self.assertEqual(len(res["skipped"]), 2)

    def test_enqueue_marks_queued_and_worker_writes_success(self):
        written = []

        def fake_get_account(acc_id):
            return _acc(acc_id)

        def fake_merge(acc_id, patch):
            written.append((acc_id, patch))

        with mock.patch("core.db.get_account", side_effect=fake_get_account), \
             mock.patch("core.db.merge_account_extra", side_effect=fake_merge), \
             mock.patch("core.codex_oauth.run_codex_oauth", return_value={
                 "status": "success", "message": "plan=free", "file_path": "/tmp/codex-a@b.com.json",
             }) as run_mock:
            res = svc.enqueue_accounts([1])
            self.assertEqual(res["queued"], [1])
            # 等后台线程跑完
            for _ in range(200):
                if not svc._ENQUEUED:
                    break
                import time
                time.sleep(0.02)
        run_mock.assert_called_once()
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], "a@b.com")
        self.assertEqual(kwargs.get("proxy"), "socks5://127.0.0.1:7897")
        self.assertTrue(kwargs.get("force"))
        # 最终写入了 success 状态
        final = [p for _, p in written if p.get("codex_oauth", {}).get("status") == "success"]
        self.assertTrue(final, f"written={written}")
        self.assertEqual(final[-1]["codex_oauth"]["filename"], "codex-a@b.com.json")

    def test_enqueue_dedup(self):
        with mock.patch("core.db.get_account", side_effect=lambda i: _acc(i)), \
             mock.patch("core.db.merge_account_extra"), \
             mock.patch("core.codex_oauth_service._EXECUTOR") as ex:
            svc._ENQUEUED.add(1)
            res = svc.enqueue_accounts([1])
            self.assertEqual(res["queued"], [])
            self.assertEqual(res["skipped"][0]["reason"], "已在队列中")


class RecoverTests(unittest.TestCase):
    def test_recover_marks_interrupted_failed(self):
        accs = [
            _acc(1, extra={"codex_oauth": {"status": "running"}}),
            _acc(2, email="c@d.com", extra={"codex_oauth": {"status": "success"}}),
        ]
        written = []
        with mock.patch("core.db.list_accounts", return_value=accs), \
             mock.patch("core.db.get_account", side_effect=lambda i: accs[i - 1]), \
             mock.patch("core.db.merge_account_extra", side_effect=lambda i, p: written.append((i, p))):
            n = svc.recover_interrupted()
        self.assertEqual(n, 1)
        self.assertEqual(written[0][1]["codex_oauth"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
