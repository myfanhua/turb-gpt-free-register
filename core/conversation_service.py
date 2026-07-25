# -*- coding: utf-8 -*-
"""会话池后台执行器：小线程池异步跑 run_binding，提供运行状态查询。

设计约束：
- 只在显式开启 PROTOCOL_CONVERSATION_ENABLE 后，真实 transport 才会发请求；
- 提交即返回，运行结果通过 conversation_manager 的 binding checkpoint 读取；
- 同一 account_id:template_id 同时只允许一个在跑，重复提交返回 busy。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conversation-pool")
_LOCK = threading.Lock()
_RUNNING: set[str] = set()

# 允许被后台拾起执行的状态；completed/running 不会重复执行。
_RUNNABLE_STATUSES = {"queued"}


def _key(account_id: int, template_id: str) -> str:
    return f"{int(account_id)}:{template_id}"


def is_running(account_id: int, template_id: str) -> bool:
    with _LOCK:
        return _key(account_id, template_id) in _RUNNING


def running_keys() -> list[str]:
    with _LOCK:
        return sorted(_RUNNING)


def _rng_delay(min_s, max_s) -> float:
    """[lo, hi] 闭区间随机秒数；hi<=0 时不等待。"""
    lo, hi = float(min_s or 0), float(max_s or 0)
    if hi < lo:
        lo, hi = hi, lo
    if hi <= 0:
        return 0.0
    import random
    return random.uniform(lo, hi)


def run_binding_async(account_id: int, template_id: str, *, retry: bool = False, timeout: int | None = None, transport_factory=None, auto: bool = False, start_delay: float | None = None, message_delay=None) -> dict:
    """提交后台执行一个绑定。重复提交返回 {"accepted": False, "busy": True}。

    风控抑制：
    - 启动前随机等待（auto=True 用注册后较长的等待区间；手动运行用短抖动）；
    - 消息之间随机等待（message_delay 可调；默认取配置的拟人化区间）。
    测试可传 start_delay=0, message_delay=lambda: 0 关闭等待。
    """
    from config import conversation_pool as cfg

    key = _key(account_id, template_id)
    with _LOCK:
        if key in _RUNNING:
            return {"accepted": False, "busy": True, "key": key}
        _RUNNING.add(key)

    def _job():
        try:
            from core import conversation_runner
            transport = transport_factory() if transport_factory else None
            if start_delay is not None:
                wait = float(start_delay)
            else:
                if auto:
                    wait = _rng_delay(cfg.CONVERSATION_POOL_START_DELAY_MIN, cfg.CONVERSATION_POOL_START_DELAY_MAX)
                else:
                    wait = _rng_delay(cfg.CONVERSATION_POOL_MANUAL_START_DELAY_MIN, cfg.CONVERSATION_POOL_MANUAL_START_DELAY_MAX)
            if wait > 0:
                time.sleep(wait)
            if message_delay is not None:
                delay_fn = message_delay
            else:
                # 生产路径硬下限 30 秒（对齐 chatgpt2api 实测节奏）：即使配置调低也不突破；
                # 测试显式传 message_delay=lambda: 0 可关闭等待。
                lo = max(int(getattr(cfg, "CONVERSATION_POOL_MESSAGE_DELAY_MIN", 0) or 0), 30)
                hi = max(int(getattr(cfg, "CONVERSATION_POOL_MESSAGE_DELAY_MAX", 0) or 0), lo)
                delay_fn = lambda: _rng_delay(lo, hi)
            conversation_runner.run_binding(
                int(account_id),
                str(template_id),
                transport=transport,
                retry=retry,
                timeout=int(timeout or getattr(cfg, "CONVERSATION_POOL_TIMEOUT", 30) or 30),
                message_delay=delay_fn,
            )
        except Exception:
            # run_binding 自身会把业务异常写进 binding checkpoint；
            # 这里兜底防止线程异常静默丢失。
            import logging
            logging.getLogger(__name__).exception("[ConversationPool] 后台执行异常: %s", key)
        finally:
            with _LOCK:
                _RUNNING.discard(key)

    _EXECUTOR.submit(_job)
    return {"accepted": True, "busy": False, "key": key}


def run_pending_async(*, template_id: str | None = None, retry_failed: bool = False, timeout: int | None = None) -> dict:
    """拾起所有 queued 绑定后台执行；可选把 failed/partial 一并重试。"""
    from core import conversation_manager as cm

    statuses = set(_RUNNABLE_STATUSES)
    if retry_failed:
        statuses |= {"failed", "partial", "needs_browser_verification"}
    submitted, busy, skipped = [], [], []
    for row in cm.list_bindings():
        if template_id and row.get("template_id") != template_id:
            continue
        status = str(row.get("status") or "")
        if status not in statuses:
            skipped.append({"key": _key(row["account_id"], row["template_id"]), "status": status})
            continue
        retry = status != "queued"
        result = run_binding_async(int(row["account_id"]), str(row["template_id"]), retry=retry, timeout=timeout)
        (submitted if result.get("accepted") else busy).append(result["key"])
    return {"submitted": submitted, "submitted_count": len(submitted), "busy": busy, "skipped": skipped}
