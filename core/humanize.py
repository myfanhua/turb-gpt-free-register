# -*- coding: utf-8 -*-
"""任务级人工操作节奏。

每个注册任务都使用独立 RNG、动作账本和可注入时钟，避免并发任务共享
模块级 random 状态。节奏层只决定等待、鼠标路径和输入分段，不改变业务状态机。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import logging
import math
import random
import secrets
import time
from typing import Callable, Iterator, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PacingPolicy:
    enabled: bool = True
    factor: float = 1.0
    profile: str = "balanced"
    browser_actions: bool = True
    review_before_submit: bool = True
    mouse_trajectory: bool = True
    review_probability: float | None = None
    ranges: Mapping[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def tempo_multiplier(self) -> float:
        return {"fast": 0.78, "balanced": 1.0, "careful": 1.12}.get(self.profile, 1.0)

    @property
    def submit_review_probability(self) -> float:
        if self.review_probability is not None:
            return max(0.0, min(1.0, float(self.review_probability)))
        return {"fast": 0.25, "balanced": 0.68, "careful": 0.92}.get(self.profile, 0.68)

    @classmethod
    def from_config(cls) -> "PacingPolicy":
        try:
            from config import humanize as cfg

            profile = str(getattr(cfg, "HUMANIZE_PACING_PROFILE", "balanced") or "balanced").strip().lower()
            if profile not in {"fast", "balanced", "careful"}:
                profile = "balanced"
            ranges = dict(getattr(cfg, "HUMANIZE_DELAYS", {}) or {})
            return cls(
                enabled=bool(getattr(cfg, "ENABLE_HUMANIZE_DELAY", True)),
                factor=max(0.0, float(getattr(cfg, "HUMANIZE_DELAY_FACTOR", 1.0) or 1.0)),
                profile=profile,
                browser_actions=bool(getattr(cfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True)),
                review_before_submit=bool(getattr(cfg, "HUMANIZE_REVIEW_BEFORE_SUBMIT", True)),
                mouse_trajectory=bool(getattr(cfg, "HUMANIZE_MOUSE_TRAJECTORY", True)),
                ranges=ranges,
            )
        except Exception:
            return cls(ranges={"api": (0.4, 1.2)})


@dataclass(frozen=True)
class PacingDecision:
    kind: str
    stage: str
    seconds: float
    ordinal: int
    skipped: bool = False

    def record(self) -> dict:
        # 只保留节奏元数据，不记录输入文本、邮箱、OTP、密码或代理。
        return {
            "kind": self.kind,
            "stage": self.stage,
            "seconds": round(self.seconds, 4),
            "ordinal": self.ordinal,
            "skipped": self.skipped,
        }


class PacingContext:
    def __init__(
        self,
        *,
        seed: int | None = None,
        policy: PacingPolicy | None = None,
        clock=None,
        interrupt_check: Callable[[], None] | None = None,
    ):
        self.seed = int(seed if seed is not None else secrets.randbits(64))
        self.rng = random.Random(self.seed)
        self.policy = policy or PacingPolicy.from_config()
        self.clock = clock or time
        self.interrupt_check = interrupt_check
        self.stage = "setup"
        self.ledger: list[dict] = []
        self.pointer: tuple[float, float] | None = None
        self._ordinal = 0
        self._reviewed: set[tuple[str, str]] = set()

    def set_stage(self, stage: str) -> None:
        value = str(stage or "").strip()
        if value:
            self.stage = value

    def _range(self, kind: str, minimum: float | None, maximum: float | None) -> tuple[float, float]:
        default = self.policy.ranges.get(kind, (0.4, 1.2))
        lo = float(default[0] if minimum is None else minimum)
        hi = float(default[1] if maximum is None else maximum)
        factor = self.policy.factor * self.policy.tempo_multiplier
        lo = max(0.0, lo * factor)
        hi = max(lo, hi * factor)
        return lo, hi

    def decide(
        self,
        kind: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> PacingDecision:
        self._ordinal += 1
        if not self.policy.enabled:
            return PacingDecision(kind, self.stage, 0.0, self._ordinal, skipped=True)
        lo, hi = self._range(kind, minimum, maximum)
        if hi <= lo:
            seconds = lo
        else:
            # 大多数动作落在区间前半段，少量出现较长的阅读/观察尾部，
            # 避免每次都是 uniform 产生的“均匀随机”节拍。
            ratio = self.rng.betavariate(2.1, 4.6)
            if self.rng.random() < 0.08:
                ratio = max(ratio, self.rng.uniform(0.68, 1.0))
            seconds = lo + (hi - lo) * ratio
        return PacingDecision(kind, self.stage, float(seconds), self._ordinal)

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        if remaining <= 0:
            return
        sleeper = getattr(self.clock, "sleep", self.clock)
        if self.interrupt_check is None:
            sleeper(remaining)
            return
        # 长停顿拆分为小片，使“停止任务”不用等待整段 sleep 结束。
        while remaining > 0:
            self.interrupt_check()
            step = min(0.25, remaining)
            sleeper(step)
            remaining -= step
        self.interrupt_check()

    def wait(
        self,
        kind: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        decision = self.decide(kind, minimum=minimum, maximum=maximum)
        self.ledger.append(decision.record())
        self._sleep(decision.seconds)
        logger.debug(
            "[Humanize] stage=%s kind=%s seconds=%.2f ordinal=%s",
            decision.stage,
            decision.kind,
            decision.seconds,
            decision.ordinal,
        )
        return decision.seconds

    def sleep_exact(self, seconds: float) -> None:
        self._sleep(seconds)

    def review(self, label: str) -> float:
        if not self.policy.enabled or not self.policy.review_before_submit:
            return 0.0
        key = (self.stage, str(label or "submit"))
        if key in self._reviewed:
            return 0.0
        self._reviewed.add(key)
        if self.rng.random() > self.policy.submit_review_probability:
            return 0.0
        return self.wait("review")

    def typing_chunks(self, text: str, *, precise: bool = False) -> list[str]:
        value = str(text)
        if precise:
            return list(value)
        chunks: list[str] = []
        index = 0
        while index < len(value):
            roll = self.rng.random()
            size = 3 if roll < 0.06 else 2 if roll < 0.30 else 1
            size = min(size, len(value) - index)
            # 标点/分隔符后自然地结束当前小段。
            for offset in range(size):
                if value[index + offset] in "@._- ":
                    size = offset + 1
                    break
            chunks.append(value[index:index + size])
            index += size
        return chunks

    def mouse_path(
        self,
        start: tuple[float, float],
        target: tuple[float, float],
    ) -> list[tuple[float, float]]:
        sx, sy = map(float, start)
        tx, ty = map(float, target)
        if not self.policy.mouse_trajectory:
            self.pointer = (tx, ty)
            return [(tx, ty)]
        steps = self.rng.randint(4, 7)
        dx, dy = tx - sx, ty - sy
        distance = max(1.0, math.hypot(dx, dy))
        perpendicular_x, perpendicular_y = -dy / distance, dx / distance
        bend = self.rng.uniform(-0.12, 0.12) * min(distance, 260.0)
        cx = (sx + tx) / 2.0 + perpendicular_x * bend
        cy = (sy + ty) / 2.0 + perpendicular_y * bend
        points: list[tuple[float, float]] = []
        for index in range(1, steps + 1):
            t = index / steps
            inv = 1.0 - t
            x = inv * inv * sx + 2.0 * inv * t * cx + t * t * tx
            y = inv * inv * sy + 2.0 * inv * t * cy + t * t * ty
            points.append((float(round(x, 3)), float(round(y, 3))))
        points[-1] = (tx, ty)
        self.pointer = (tx, ty)
        return points


_ACTIVE_PACING: ContextVar[PacingContext | None] = ContextVar("active_pacing", default=None)


def current_pacing(*, create: bool = True) -> PacingContext | None:
    context = _ACTIVE_PACING.get()
    if context is None and create:
        context = PacingContext()
        _ACTIVE_PACING.set(context)
    return context


@contextmanager
def pacing_scope(
    *,
    seed: int | None = None,
    policy: PacingPolicy | None = None,
    clock=None,
    interrupt_check: Callable[[], None] | None = None,
) -> Iterator[PacingContext]:
    context = PacingContext(
        seed=seed,
        policy=policy,
        clock=clock,
        interrupt_check=interrupt_check,
    )
    token = _ACTIVE_PACING.set(context)
    logger.debug("[Humanize] task pacing started profile=%s", context.policy.profile)
    try:
        yield context
    finally:
        logger.debug("[Humanize] task pacing finished actions=%s", len(context.ledger))
        _ACTIVE_PACING.reset(token)


def paced_task(func=None, *, interrupt_check: Callable[[], None] | None = None):
    def decorate(target):
        @wraps(target)
        def wrapped(*args, **kwargs):
            with pacing_scope(interrupt_check=interrupt_check):
                return target(*args, **kwargs)
        return wrapped

    return decorate if func is None else decorate(func)


def set_stage(stage: str) -> None:
    context = current_pacing()
    assert context is not None
    context.set_stage(stage)


def pacing_rng() -> random.Random:
    context = current_pacing()
    assert context is not None
    return context.rng


def pacing_sleep(seconds: float) -> None:
    context = current_pacing()
    assert context is not None
    context.sleep_exact(seconds)


def delay(
    kind: str = "api",
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """根据当前任务的节奏策略等待，返回实际秒数。"""
    context = current_pacing()
    assert context is not None
    return context.wait(kind, minimum=minimum, maximum=maximum)
