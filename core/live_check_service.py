# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import check_account_plan, resolve_plan_check_route
from core.plan_check_service import resolve_account_plan_proxy

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _needs_full_login(result: dict | None) -> bool:
    result = result or {}
    return bool(
        result.get("needs_live_check")
        or result.get("token_expired") is True
        or result.get("http_status") == 401
    )


def _is_proxy_transport_failure(result: dict | None) -> bool:
    result = result or {}
    if result.get("ok") or str(result.get("status") or "").lower() == "deactivated":
        return False
    error = str(result.get("error") or "").lower()
    return any(hint in error for hint in (
        "403", "proxy", "socks", "ssl", "timeout", "timed out",
        "connection", "closed", "reset", "dns",
    ))


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}

        account = db.get_account(account_id) or {}
        saved_token = str(account.get("access_token") or "").strip()
        selected_proxy, proxy_context = resolve_account_plan_proxy(
            account_id=account_id,
            email=email,
            proxy=proxy,
        )
        if saved_token:
            _append_log(email, "[查活] 优先验证账号已保存 AT，不重新触发邮箱登录")
            token_result = check_account_plan(saved_token, proxy=selected_proxy)
            if token_result.get("ok"):
                result = {
                    "ok": True,
                    "status": "live",
                    "source": "stored_at",
                    "checked_at": token_result.get("checked_at") or datetime.now().isoformat(timespec="seconds"),
                    "access_token": saved_token,
                    "http_status": token_result.get("http_status"),
                    "network_route": token_result.get("network_route"),
                    "proxy_used": token_result.get("proxy_used"),
                    "proxy_fallback_reason": token_result.get("proxy_fallback_reason"),
                    "plan_check_proxy_provider": token_result.get("plan_check_proxy_provider")
                    or (proxy_context or {}).get("proxy_provider"),
                    "plan_check_proxy_country": token_result.get("plan_check_proxy_country")
                    or (proxy_context or {}).get("proxy_country"),
                }
                db.update_account_liveness(account_id, result)
                _append_log(email, "[查活] 完成：保存 AT 有效，账号正常")
                return result
            if not _needs_full_login(token_result):
                result = {
                    **token_result,
                    "ok": False,
                    "status": "failed",
                    "source": "stored_at",
                    "error": f"保存 AT 验证失败：{token_result.get('error') or '未知网络错误'}",
                }
                db.update_account_liveness(account_id, result)
                _append_log(email, f"[查活] 完成：失败 {result['error']}")
                return result
            _append_log(email, "[查活] 保存 AT 已失效，继续完整登录刷新 AT")

        route = resolve_plan_check_route(explicit_proxy=selected_proxy)
        selected_proxy = route.get("proxy")
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        result = check_account_liveness(email, proxy=selected_proxy, clear_log=False)
        # auto 模式下代理若在 HTTP 响应前失败，则真实直连兜底一次。
        # 显式 proxy 覆盖属于 request 模式，不改变调用方指定的网络路径。
        if (
            not result.get("ok")
            and result.get("status") == "failed"
            and _is_proxy_transport_failure(result)
            and selected_proxy
            and str(route.get("network_route") or "") == "proxy"
            and str(route.get("proxy_mode") or "") == "auto"
        ):
            _append_log(email, "[查活] 代理传输失败，auto 模式切换真实直连重试一次")
            result = check_account_liveness(email, proxy="", clear_log=False)
        db.update_account_liveness(account_id, result)
        if result.get("ok"):
            _append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
        elif result.get("status") == "deactivated":
            _append_log(email, f"[查活] 完成：账号已废 {result.get('error') or ''}")
        else:
            _append_log(email, f"[查活] 完成：失败 {result.get('error') or ''}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

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
