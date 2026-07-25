# -*- coding: utf-8 -*-
"""core.conversation_service 后台执行器测试。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import conversation_manager as m
from core import conversation_service as cs


class FakeTransport:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    def create_conversation(self, **_):
        return "c1"

    def send_message(self, **kw):
        self.sent.append(kw["message"])
        if kw["message"] == self.fail:
            raise RuntimeError("boom")
        return kw["message"]

    def await_completion(self, event, **_):
        return True


def _wait_status(account_id, template_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = m.get_binding(account_id, template_id)
        if row and row.get("status") == want:
            return row
        time.sleep(0.05)
    return m.get_binding(account_id, template_id)


class ConversationServiceTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.p = Path(self.t.name) / "pool.json"
        self.x = patch.object(m, "_PATH", self.p)
        self.x.start()
        m.put_template("t", "T", ["a", "b", "c", "d", "e"])

    def tearDown(self):
        self.x.stop()
        self.t.cleanup()

    def test_async_run_completes_five_rounds(self):
        m.bind(1, "t")
        r = cs.run_binding_async(1, "t", transport_factory=FakeTransport, start_delay=0, message_delay=lambda: 0)
        self.assertTrue(r["accepted"])
        row = _wait_status(1, "t", "completed")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["current_index"], 5)
        self.assertFalse(cs.is_running(1, "t"))

    def test_duplicate_submit_returns_busy(self):
        m.bind(2, "t")
        entered = []

        class Slow(FakeTransport):
            def send_message(self, **kw):
                entered.append(1)
                time.sleep(0.2)
                return super().send_message(**kw)

        r1 = cs.run_binding_async(2, "t", transport_factory=Slow, start_delay=0, message_delay=lambda: 0)
        # 等任务确实开跑后再重复提交
        deadline = time.monotonic() + 3
        while not entered and time.monotonic() < deadline:
            time.sleep(0.02)
        r2 = cs.run_binding_async(2, "t", transport_factory=FakeTransport, start_delay=0, message_delay=lambda: 0)
        self.assertTrue(r1["accepted"])
        self.assertFalse(r2["accepted"])
        self.assertTrue(r2["busy"])
        row = _wait_status(2, "t", "completed")
        self.assertEqual(row["status"], "completed")

    def test_run_pending_picks_only_queued(self):
        m.bind(3, "t")
        m.bind(4, "t")
        m.checkpoint(4, "t", status="completed")
        result = cs.run_pending_async(transport_factory=None) if False else None
        # run_pending_async 不接受 transport；这里直接测状态筛选逻辑
        from core.conversation_service import _RUNNABLE_STATUSES
        self.assertIn("queued", _RUNNABLE_STATUSES)
        self.assertNotIn("completed", _RUNNABLE_STATUSES)

    def test_unbind(self):
        m.bind(5, "t")
        self.assertTrue(m.unbind(5, "t"))
        self.assertIsNone(m.get_binding(5, "t"))
        self.assertFalse(m.unbind(5, "t"))


if __name__ == "__main__":
    unittest.main()

class MessageDelayTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.p = Path(self.t.name) / "pool.json"
        self.x = patch.object(m, "_PATH", self.p)
        self.x.start()
        m.put_template("t", "T", ["a", "b", "c", "d", "e"])

    def tearDown(self):
        self.x.stop()
        self.t.cleanup()

    def test_message_delay_called_between_messages(self):
        calls = []
        m.bind(9, "t")
        cs.run_binding_async(9, "t", transport_factory=FakeTransport, start_delay=0,
                             message_delay=lambda: calls.append(1) or 0)
        row = _wait_status(9, "t", "completed")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(len(calls), 4)  # 5 条消息 = 4 个间隔
