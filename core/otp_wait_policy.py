# -*- coding: utf-8 -*-
"""可注入的 OTP 等待/重发编排基础层。

本模块当前未接入注册驱动或 provider。它定义统一轮次语义：``retry_count``
是初次发送之后允许的额外重发次数，故总轮数最多为 ``1 + retry_count``。
``wait`` 仅调度下一次查询，不作为邮件已经到达的判断。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


def _redact_email(email: str) -> str:
    local, sep, domain = str(email or "").partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"


@dataclass
class OTPWaitExhausted(RuntimeError):
    provider: str
    email: str
    rounds: int
    last_error: str | None = None

    def __str__(self) -> str:
        detail = f"; last_error={self.last_error}" if self.last_error else ""
        return f"OTP 等待耗尽: provider={self.provider}, email={_redact_email(self.email)}, rounds={self.rounds}{detail}"


@dataclass
class OTPProbeResult:
    """provider 单次 probe 的结果；state 由 provider 持有并跨 probe 保留。"""
    status: str = "pending"  # pending / candidate / completed
    code: str | None = None
    state: object | None = None
    ready_at_monotonic: float | None = None

    @classmethod
    def completed(cls, code: str, state=None): return cls("completed", code, state)
    @classmethod
    def pending(cls, state=None): return cls("pending", None, state)
    @classmethod
    def candidate(cls, state=None, ready_at_monotonic=None): return cls("candidate", None, state, ready_at_monotonic)


def resolve_wait_timeout(otp_wait_timeout: int | None, legacy_otp_max_wait: int | None) -> int:
    """新配置优先；未配置时兼容旧 OTP_MAX_WAIT。"""
    value = otp_wait_timeout if otp_wait_timeout is not None else legacy_otp_max_wait
    return max(1, int(value or 1))


def wait_for_otp_with_policy(
    *,
    provider: str,
    email: str,
    initial_after_ts: float,
    fetch: Callable[[float], str | None],
    retrigger: Callable[[], float],
    wait_timeout: int,
    poll_interval: int,
    retry_count: int,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> str:
    """按统一策略等待 OTP；``fetch`` 每次只做一次 provider 查询。"""
    timeout = max(1, int(wait_timeout))
    interval = max(1, int(poll_interval))
    total_rounds = 1 + max(0, int(retry_count))
    after_ts = float(initial_after_ts)
    last_error: str | None = None

    for round_no in range(1, total_rounds + 1):
        deadline = clock() + timeout
        attempted = False
        while True:
            # 每轮允许立即首查；首查之后到达 deadline 就进入下一轮/结束，
            # 避免在边界重复查询一次。
            if attempted and clock() >= deadline:
                break
            attempted = True
            candidate_ready_at = None
            try:
                otp = fetch(after_ts)
                if isinstance(otp, OTPProbeResult):
                    if otp.status == "completed" and otp.code:
                        return str(otp.code)
                    if otp.status not in {"pending", "candidate"}:
                        raise ValueError(f"未知 OTP probe 状态: {otp.status}")
                    candidate_ready_at = otp.ready_at_monotonic if otp.status == "candidate" else None
                elif otp:
                    return str(otp)
                last_error = "provider 未返回匹配 OTP"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:180]}"

            remaining = deadline - clock()
            if remaining <= 0:
                break
            delay = min(interval, remaining)
            if candidate_ready_at is not None:
                delay = min(delay, max(0.0, candidate_ready_at - clock()))
                if delay <= 0:
                    # 候选已可再判定，但仍让 provider probe 决定；避免零等待忙循环。
                    delay = min(interval, remaining)
            wait(delay)

        if round_no < total_rounds:
            try:
                after_ts = float(retrigger())
            except Exception as exc:
                last_error = f"重发失败: {type(exc).__name__}: {str(exc)[:180]}"
                break

    raise OTPWaitExhausted(provider=provider, email=email, rounds=round_no, last_error=last_error)
