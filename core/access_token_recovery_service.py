# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import proxy as proxy_cfg
from core import db
from core.roxy_access_token_recovery import (
    AccessTokenRecoveryStopped,
    run_roxy_access_token_recovery,
)

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_WORKERS = max(1, min(16, int(getattr(proxy_cfg, "PLAN_CHECK_WORKERS", 3) or 3)))
_QUEUE_LIMIT = max(
    _WORKERS,
    min(5000, int(getattr(proxy_cfg, "PLAN_CHECK_QUEUE_LIMIT", 500) or 500)),
)
_EXECUTOR = ThreadPoolExecutor(
    max_workers=_WORKERS,
    thread_name_prefix="access-token-recovery",
)
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_EVENTS_LOCK = threading.Lock()
_STOP_EVENTS: dict[int, threading.Event] = {}


class _CurrentThreadOnly(logging.Filter):
    def __init__(self):
        super().__init__()
        self.thread_id = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return int(record.thread) == self.thread_id


class _RecoveryLogContext:
    def __init__(self, path: str):
        self.path = Path(path)
        self.handler = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.path, encoding="utf-8")
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        self.handler.addFilter(_CurrentThreadOnly())
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()


def _sanitize_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(
        r"(?i)(https?://)([^/@:\s]+):([^/@\s]+)@",
        r"\1***:***@",
        text,
    )
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[redacted-jwt]",
        text,
    )
    text = re.sub(
        r"(?i)\b(authorization|access[_-]?token|token)\b(\s*[=:]\s*)(?:Bearer\s+)?([^\s,;]+)",
        r"\1\2[redacted]",
        text,
    )
    return text[:500]


def _event_for(account_id: int) -> threading.Event:
    with _EVENTS_LOCK:
        return _STOP_EVENTS.setdefault(int(account_id), threading.Event())


def _should_stop(account_id: int) -> bool:
    with _EVENTS_LOCK:
        event = _STOP_EVENTS.get(int(account_id))
    return bool(event and event.is_set()) or db.is_account_access_token_recovery_stop_requested(
        account_id
    )


def _run_recovery(*, account_id: int, trigger: str) -> dict:
    try:
        if not db.mark_account_access_token_recovery_running(account_id):
            account = db.get_account(account_id) or {}
            if account.get("at_recovery_status") == "stopped":
                return {"ok": False, "status": "stopped", "error": "用户手动停止"}
            return {
                "ok": False,
                "status": "failed",
                "error": "账号已删除或补 AT 状态已重置",
            }

        account = db.get_account(account_id)
        if not account:
            raise RuntimeError("账号不存在")
        if str(account.get("access_token") or "").strip():
            complete = db.complete_account_access_token_recovery(
                account_id,
                session_info={"accessToken": str(account.get("access_token"))},
                device_id=str(account.get("device_id") or ""),
                proxy_used=account.get("proxy_used"),
            )
            return {"ok": True, "status": "success", **complete}

        proxy = (
            str(account.get("proxy_used") or "").strip()
            or str(proxy_cfg.pick_proxy() or "").strip()
            or None
        )
        device_id = str(account.get("device_id") or "").strip() or str(uuid.uuid4())
        result = run_roxy_access_token_recovery(
            email=str(account.get("email") or ""),
            proxy=proxy,
            device_id=device_id,
            should_stop=lambda: _should_stop(account_id),
        )
        persisted = db.complete_account_access_token_recovery(
            account_id,
            session_info=result["session_info"],
            device_id=result["device_id"],
            proxy_used=result.get("proxy_used"),
        )
        return {"ok": True, "status": "success", **persisted}
    except AccessTokenRecoveryStopped as exc:
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(account_id, error=error, status="stopped")
        return {"ok": False, "status": "stopped", "error": error}
    except Exception as exc:
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(account_id, error=error, status="failed")
        logger.error("[补AT] 账号恢复失败: account_id=%s error=%s", account_id, error)
        return {"ok": False, "status": "failed", "error": error}


def _run_recovery_with_log(*, account_id: int, trigger: str, log_file: str) -> dict:
    try:
        with _RecoveryLogContext(log_file):
            logger.info("[补AT] 开始: account_id=%s trigger=%s", account_id, trigger)
            result = _run_recovery(account_id=account_id, trigger=trigger)
            logger.info(
                "[补AT] 完成: account_id=%s status=%s",
                account_id,
                result.get("status"),
            )
            return result
    finally:
        with _EVENTS_LOCK:
            _STOP_EVENTS.pop(int(account_id), None)
        _QUEUE_SLOTS.release()


def enqueue_account_access_token_recovery(
    *,
    account_id: int,
    trigger: str = "manual",
) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {
            "accepted": False,
            "busy": False,
            "skipped": False,
            "error": "补 AT 队列已满",
        }
    log_file = str(_LOG_DIR / f"at-recovery-{int(account_id)}-{uuid.uuid4().hex}.log")
    claim = db.claim_account_access_token_recovery(
        int(account_id),
        trigger=trigger,
        log_file=log_file,
    )
    if not claim.get("accepted"):
        _QUEUE_SLOTS.release()
        return claim
    _event_for(int(account_id))
    try:
        future = _EXECUTOR.submit(
            _run_recovery_with_log,
            account_id=int(account_id),
            trigger=trigger,
            log_file=log_file,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        with _EVENTS_LOCK:
            _STOP_EVENTS.pop(int(account_id), None)
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(int(account_id), error=error)
        return {"accepted": False, "busy": False, "skipped": False, "error": error}
    return {**claim, "future": future}


def request_stop(account_id: int) -> dict:
    result = db.request_account_access_token_recovery_stop(int(account_id))
    if result.get("stopped"):
        _event_for(int(account_id)).set()
    return result


def request_stop_bulk(account_ids: list[int]) -> dict:
    stopped = []
    skipped = []
    seen = set()
    for raw in account_ids:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            skipped.append({"id": raw, "reason": "ID 非法"})
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        result = request_stop(account_id)
        if result.get("stopped"):
            stopped.append({"id": account_id, **result})
        else:
            skipped.append({"id": account_id, "reason": result.get("error") or "未停止"})
    return {
        "stopped": stopped,
        "stopped_count": len(stopped),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


def read_log(account_id: int, max_bytes: int = 50_000) -> str:
    account = db.get_account(int(account_id))
    raw_path = str((account or {}).get("at_recovery_log_file") or "").strip()
    if not raw_path:
        return ""
    path = Path(raw_path).resolve()
    log_root = _LOG_DIR.resolve()
    if path.parent != log_root or not path.name.startswith("at-recovery-") or not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max(1, int(max_bytes))))
        return handle.read().decode("utf-8", errors="replace")


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
