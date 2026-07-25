# -*- coding: utf-8 -*-
"""Turnstile 求解器离线测试：单层流与新版嵌套流（套娃）都要解出结果。"""
import base64
import json
import unittest

from core.turnstile_solver import _xor_string, solve_turnstile_token


def _make_dx(token_list, p: str) -> str:
    raw = json.dumps(token_list)
    return base64.b64encode(_xor_string(raw, p).encode()).decode()


class TurnstileSolverTest(unittest.TestCase):
    P = "gAAAAACfake-ptoken~S"

    def test_single_pass_stream(self):
        # 旧版：主指令流直接调用 func_3 产出结果
        dx = _make_dx([[3, "hello-result"]], self.P)
        self.assertEqual(solve_turnstile_token(dx, self.P), base64.b64encode(b"hello-result").decode())

    def test_nested_stream(self):
        # 新版套娃：主流程把 pm[9] 换成二层指令流，二层再产出结果
        nested = [[3, "nested-result"]]
        dx = _make_dx([[2, 9, nested]], self.P)
        self.assertEqual(solve_turnstile_token(dx, self.P), base64.b64encode(b"nested-result").decode())

    def test_multi_level_nested_stream(self):
        level2 = [[3, "deep-result"]]
        level1 = [[2, 9, level2]]
        dx = _make_dx([[2, 9, level1]], self.P)
        self.assertEqual(solve_turnstile_token(dx, self.P), base64.b64encode(b"deep-result").decode())

    def test_garbage_returns_none(self):
        self.assertIsNone(solve_turnstile_token("!!!not-base64!!!", self.P))

    def test_wrong_key_returns_none(self):
        dx = _make_dx([[3, "x"]], self.P)
        self.assertIsNone(solve_turnstile_token(dx, "wrong-key"))


if __name__ == "__main__":
    unittest.main()
