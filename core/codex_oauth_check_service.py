# -*- coding: utf-8 -*-
"""Codex OAuth 凭证实时测活后台队列。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core import codex_oauth, db

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="codex-oauth-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_codex_oauth_check(*, account_id: int, email: str, check_id: str) -> dict:
    try:
        if not db.mark_account_codex_oauth_check_running(account_id, expected_check_id=check_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或测活状态已被重置"}
        result = codex_oauth.check_cpa_codex_oauth(email=email)
        db.update_account_codex_oauth_check(account_id, result, expected_check_id=check_id)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "http_status": None,
            "checked_at": _checked_at(),
            "error": f"{type(exc).__name__}: {str(exc)[:700]}",
            "cpa_name": None,
        }
        try:
            db.update_account_codex_oauth_check(account_id, result, expected_check_id=check_id)
        except Exception:
            logger.exception("[Codex OAuth测活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[Codex OAuth测活] 后台异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_codex_oauth_check(*, account_id: int, email: str, trigger: str = "manual") -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "Codex OAuth 测活队列已满，请稍后重试"}
    if not db.claim_account_codex_oauth_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在检测 Codex OAuth"}
    account = db.get_account(account_id) or {}
    check_id = str(account.get("codex_oauth_check_id") or "")
    if not check_id:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": False, "error": "Codex OAuth 测活任务标识缺失"}
    try:
        _EXECUTOR.submit(_run_codex_oauth_check, account_id=account_id, email=email, check_id=check_id)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        error = f"Codex OAuth 测活入队失败: {type(exc).__name__}: {str(exc)[:300]}"
        db.update_account_codex_oauth_check(account_id, {
            "ok": False,
            "status": "failed",
            "checked_at": _checked_at(),
            "error": error,
        }, expected_check_id=check_id)
        return {"accepted": False, "busy": False, "error": error}
    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
