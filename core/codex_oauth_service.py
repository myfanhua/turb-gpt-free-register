# -*- coding: utf-8 -*-
"""Codex OAuth（真凭证·接码）后台队列。

为什么需要这个模块（2026-07-25 实测结论）：
- 已删除的「免手机验证 synthetic 转化」产出的 auth.json 用的是 web session accessToken
  （client_id=app_X8zY6vW2...），调 /backend-api/codex/responses 会被 OpenAI 后端
  返回 401 {"detail":"Unauthorized"}；在 Cockpit(CLIProxyAPI) 里表现为 429/冷却。
- 只有走真 Codex CLI OAuth 流程（client_id=app_EMoamEEZ...，含真 refresh_token）
  的凭证才能被 Cockpit / Sub2API 直接使用。
- 该流程对新账号要求手机短信验证，因此需要接码服务（本地 L 服务或 GrizzlySMS）。

本模块提供：
- enqueue_accounts：把选中账号排入后台队列，逐个跑 core.codex_oauth.run_codex_oauth
  （force=True，使用账号注册时的 proxy_used，降低 IP 突变风控）。
- preflight：运行前自检（授权地址来源 / 接码服务可达性 / 必要的 key）。
- recover_interrupted：WebUI 重启后把 queued/running 状态标记为失败。
状态写入 accounts.extra_json.codex_oauth，由账号列表接口下发给前端。
"""
from __future__ import annotations

import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from core import db

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-oauth")
_QUEUE_SLOTS = threading.BoundedSemaphore(200)
_LOCK = threading.Lock()
_ENQUEUED: set[int] = set()

_EXTRA_KEY = "codex_oauth"


def _now() -> str:
    return db._now()


def _write_status(acc_id: int, patch: dict) -> None:
    try:
        acc = db.get_account(acc_id)
        if not acc:
            return
        extra = acc.get("extra_json") or {}
        if isinstance(extra, str):
            import json as _json
            extra = _json.loads(extra) if extra.strip() else {}
        state = extra.get(_EXTRA_KEY) if isinstance(extra, dict) else {}
        if not isinstance(state, dict):
            state = {}
        state.update(patch)
        db.merge_account_extra(int(acc_id), {_EXTRA_KEY: state})
    except Exception:
        logger.exception("[CodexOAuth] 写状态失败 acc_id=%s", acc_id)


def read_status(acc: dict) -> dict:
    """从账号行读取 codex_oauth 状态（供列表接口使用）。"""
    try:
        extra = acc.get("extra_json") or {}
        if isinstance(extra, str):
            import json as _json
            extra = _json.loads(extra) if extra.strip() else {}
        state = extra.get(_EXTRA_KEY) if isinstance(extra, dict) else {}
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def preflight() -> dict:
    """运行前自检。返回接码/授权配置与可达性，供前端在确认框里展示。"""
    from config import codex as _cfg

    auth_source = str(getattr(_cfg, "CODEX_AUTH_URL_SOURCE", "cpa") or "cpa").strip().lower()
    provider = str(getattr(_cfg, "SMS_PROVIDER", "l") or "l").strip().lower()
    warnings: list[str] = []
    info: dict = {
        "auth_url_source": auth_source,
        "sms_provider": provider,
        "oauth_driver": str(getattr(_cfg, "CODEX_OAUTH_DRIVER", "protocol") or "protocol"),
    }

    if auth_source == "cpa":
        warnings.append("CODEX_AUTH_URL_SOURCE=cpa：授权凭证将由 CPA 持有，本地不会生成可下载的 auth.json；要导入 Cockpit/Sub2API 请改为 local")
    elif auth_source == "sub2":
        warnings.append("CODEX_AUTH_URL_SOURCE=sub2：授权结果会直接推送到 Sub2API，本地不生成 auth.json")

    if provider == "l":
        base = str(getattr(_cfg, "L_API_BASE", "http://localhost:8788") or "").strip()
        info["l_api_base"] = base
        reachable = False
        try:
            parsed = urlparse(base if "://" in base else f"http://{base}")
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=2):
                reachable = True
        except Exception:
            reachable = False
        info["l_reachable"] = reachable
        if not reachable:
            warnings.append(f"本地 L 接码服务不可达（{base}）：请先启动取号服务，或在配置里改用 GrizzlySMS")
        if not str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip():
            warnings.append("L_ADMIN_AUTH_CODE 未配置：L 取号接口需要 Bearer 授权码")
    elif provider == "grizzly":
        if not str(getattr(_cfg, "SMS_API_KEY", "") or "").strip():
            warnings.append("SMS_API_KEY 未配置：GrizzlySMS 需要 API Key（且账户需有余额）")
    elif provider == "h":
        if not str(getattr(_cfg, "H_ADMIN_AUTH_CODE", "") or "").strip():
            warnings.append("H_ADMIN_AUTH_CODE 未配置：H 取号服务需要授权码")

    info["warnings"] = warnings
    info["ready"] = not any("不可达" in w or "未配置" in w for w in warnings)
    return info


def _run_one(acc_id: int) -> None:
    from core.codex_oauth import run_codex_oauth

    acc = db.get_account(acc_id)
    if not acc:
        return
    email = str(acc.get("email") or "").strip()
    if not email:
        _write_status(acc_id, {"status": "failed", "message": "账号邮箱为空", "finished_at": _now()})
        return
    proxy = str(acc.get("proxy_used") or "").strip() or None
    _write_status(acc_id, {"status": "running", "started_at": _now(), "message": ""})
    logger.info("[CodexOAuth] 开始授权 acc_id=%s email=%s proxy=%s", acc_id, email, proxy or "(pool)")
    try:
        result = run_codex_oauth(email, proxy=proxy, force=True)
    except Exception as exc:
        logger.exception("[CodexOAuth] 授权异常 acc_id=%s", acc_id)
        _write_status(acc_id, {
            "status": "failed",
            "message": f"{type(exc).__name__}: {str(exc)[:180]}",
            "finished_at": _now(),
        })
        return
    status = str((result or {}).get("status") or "failed")
    patch = {
        "status": "success" if status == "success" else status,
        "message": str((result or {}).get("message") or "")[:240],
        "finished_at": _now(),
    }
    if (result or {}).get("file_path"):
        patch["file_path"] = str(result["file_path"])
        patch["filename"] = Path(str(result["file_path"])).name
    _write_status(acc_id, patch)
    logger.info("[CodexOAuth] 授权结束 acc_id=%s status=%s", acc_id, patch["status"])


def _task(acc_id: int) -> None:
    try:
        _run_one(acc_id)
    finally:
        with _LOCK:
            _ENQUEUED.discard(acc_id)
        try:
            _QUEUE_SLOTS.release()
        except ValueError:
            pass


def enqueue_accounts(account_ids: list[int]) -> dict:
    """把账号排入授权队列。返回 {queued:[], skipped:[{id,reason}]}。"""
    queued: list[int] = []
    skipped: list[dict] = []
    for raw in account_ids:
        try:
            acc_id = int(raw)
        except (TypeError, ValueError):
            skipped.append({"id": raw, "reason": "ID 非法"})
            continue
        acc = db.get_account(acc_id)
        if not acc:
            skipped.append({"id": acc_id, "reason": "账号不存在"})
            continue
        if not str(acc.get("email") or "").strip():
            skipped.append({"id": acc_id, "reason": "邮箱为空"})
            continue
        with _LOCK:
            if acc_id in _ENQUEUED:
                skipped.append({"id": acc_id, "reason": "已在队列中"})
                continue
            _ENQUEUED.add(acc_id)
        try:
            _QUEUE_SLOTS.acquire(timeout=0.1)
        except Exception:
            with _LOCK:
                _ENQUEUED.discard(acc_id)
            skipped.append({"id": acc_id, "reason": "队列已满"})
            continue
        _write_status(acc_id, {"status": "queued", "queued_at": _now(), "message": ""})
        _EXECUTOR.submit(_task, acc_id)
        queued.append(acc_id)
    return {"queued": queued, "skipped": skipped}


def recover_interrupted() -> int:
    """WebUI 重启后，把仍处于 queued/running 的授权状态标记为失败。"""
    count = 0
    try:
        for acc in db.list_accounts(limit=5000, archived="all"):
            state = read_status(acc)
            if str(state.get("status") or "") in {"queued", "running"}:
                _write_status(int(acc["id"]), {
                    "status": "failed",
                    "message": "WebUI 重启导致授权中断，可重新发起",
                    "finished_at": _now(),
                })
                count += 1
    except Exception:
        logger.exception("[CodexOAuth] 恢复中断状态失败")
    return count
