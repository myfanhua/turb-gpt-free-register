# -*- coding: utf-8 -*-
"""试用支付方式探测后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.plan_check_service import resolve_account_plan_proxy
from core.trial_payment_probe import probe_trial_payment_methods

logger = logging.getLogger(__name__)


def _setting(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _float_setting(name: str, default: float, low: float = 0.0, high: float = 30.0) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


_WORKERS = _setting("TRIAL_PAYMENT_PROBE_WORKERS", getattr(proxy_cfg, "PLAN_CHECK_WORKERS", 3), 1, 16)
_QUEUE_LIMIT = _setting("TRIAL_PAYMENT_PROBE_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="trial-payment")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    global _NEXT_REQUEST_AT
    try:
        interval = _float_setting("TRIAL_PAYMENT_PROBE_MIN_INTERVAL", 0.8)
    except Exception:
        interval = 0.8
    jitter = _float_setting("TRIAL_PAYMENT_PROBE_JITTER", 0.2)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + interval
    if scheduled > now:
        time.sleep(scheduled - now)


def _country_from_context(context: dict | None, explicit: str | None) -> str:
    for value in (explicit, (context or {}).get("proxy_country")):
        text = str(value or "").strip().upper()
        if len(text) == 2 and text.isalpha():
            return text
    return "US"


def run_account_trial_payment_probe(
    *,
    account_id: int,
    email: str,
    access_token: str,
    proxy: str | None = None,
    country: str | None = None,
    campaign_id: str | None = None,
    trigger: str = "manual",
    _slot_acquired: bool = False,
) -> dict:
    """执行单账号探测并写回摘要；此函数也供测试/运维重放。"""
    try:
        if not db.mark_account_trial_payment_probe_running(int(account_id)):
            return {"ok": False, "error": "账号已删除或支付方式探测状态已被重置"}
        selected_proxy, context = resolve_account_plan_proxy(
            account_id=int(account_id),
            email=str(email or ""),
            proxy=proxy,
            trigger=trigger,
        )
        selected_country = _country_from_context(context, country)
        _wait_for_rate_slot()
        result = probe_trial_payment_methods(
            access_token,
            country=selected_country,
            proxy=selected_proxy,
            campaign_id=str(campaign_id or "plus-1-month-free"),
        )
        if context:
            result.setdefault("proxy_provider", context.get("proxy_provider"))
            result.setdefault("proxy_country", context.get("proxy_country"))
        db.update_account_trial_payment_probe(acc_id=int(account_id), result=result)
        if result.get("ok"):
            logger.info("[TrialPayment] 探测完成: %s country=%s", email, selected_country)
        else:
            logger.warning("[TrialPayment] 探测失败: %s error=%s", email, result.get("error") or "未知错误")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_trial_payment_probe(acc_id=int(account_id), result=result)
        except Exception:
            logger.exception("[TrialPayment] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[TrialPayment] 后台探测异常: %s", email)
        return result
    finally:
        if _slot_acquired:
            _QUEUE_SLOTS.release()


def enqueue_account_trial_payment_probe(
    *,
    account_id: int,
    email: str,
    access_token: str,
    proxy: str | None = None,
    country: str | None = None,
    campaign_id: str | None = None,
    trigger: str = "manual",
) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "试用支付方式探测队列已满，请稍后重试"}
    if not db.claim_account_trial_payment_probe(acc_id=account_id):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在探测试用支付方式"}
    try:
        _EXECUTOR.submit(
            run_account_trial_payment_probe,
            account_id=account_id,
            email=email,
            access_token=access_token,
            proxy=proxy,
            country=country,
            campaign_id=campaign_id,
            trigger=str(trigger or "manual"),
            _slot_acquired=True,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {"ok": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"支付方式探测入队失败: {type(exc).__name__}"}
        db.update_account_trial_payment_probe(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {"accepted": True, "busy": False, "account_id": account_id, "email": email, "status": "queued", "trigger": str(trigger or "manual")}


def is_probing(account_id: int) -> bool:
    account = db.get_account(int(account_id))
    return bool(account and str(account.get("trial_payment_probe_status") or "") in {"queued", "running"})


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("TRIAL_PAYMENT_PROBE_MIN_INTERVAL", 0.8),
        "jitter": _float_setting("TRIAL_PAYMENT_PROBE_JITTER", 0.2),
    }


__all__ = [
    "enqueue_account_trial_payment_probe",
    "is_probing",
    "queue_settings",
    "run_account_trial_payment_probe",
]
