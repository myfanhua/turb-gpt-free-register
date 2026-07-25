# -*- coding: utf-8 -*-
"""MailNest/迈巢临时邮箱客户端。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email
from core.otp_wait_policy import OTPProbeResult, resolve_wait_timeout, wait_for_otp_with_policy

logger = logging.getLogger(__name__)

BASE_URL = "https://mailnest.top"
REQUEST_TIMEOUT = 20


class MailNestClientError(RuntimeError):
    """MailNest 邮箱服务相关异常。"""


@dataclass
class MailNestAccount:
    email: str
    project_code: str = ""


@dataclass
class MailNestProbeState:
    best_otp: str | None = None
    best_timestamp: float = float("-inf")
    settle_until: float | None = None


_CONTEXT_CACHE: dict[str, MailNestAccount] = {}


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _api_key() -> str:
    api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
    if not api_key:
        raise MailNestClientError("MailNest API Key 未配置，请填写 MailNest API Key（WebUI「配置 → 邮箱 / OTP」）。")
    return api_key


def _project_code() -> str:
    project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
    if not project_code:
        raise MailNestClientError("MailNest 项目代码未配置，请填写 MailNest 项目代码（默认 chatgpt001）。")
    return project_code


def _request(method: str, path: str, *, params: dict | None = None, json: dict | None = None):
    try:
        resp = requests.request(
            method,
            BASE_URL + path,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MailNestClientError(f"MailNest 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise MailNestClientError(f"MailNest 响应不是 JSON ({path}): HTTP {resp.status_code}") from exc

    if resp.status_code == 401:
        raise MailNestClientError("MailNest API Key 非法或已失效")
    if resp.status_code >= 400:
        raise MailNestClientError(f"MailNest 请求失败 ({path}): HTTP {resp.status_code}; {str(payload)[:200]}")
    if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
        raise MailNestClientError(f"MailNest 请求失败 ({path}): {payload}")
    return payload.get("data")


def pick_account() -> MailNestAccount:
    """购买/领取一个 MailNest 临时邮箱并缓存上下文。"""
    project_code = _project_code()
    data = _request(
        "POST",
        "/api/v1/email/temporary/buy",
        json={"project_code": project_code, "count": 1},
    )
    if not isinstance(data, list) or not data:
        raise MailNestClientError("MailNest 购买邮箱响应缺少 data[0]")
    email = str((data[0] or {}).get("email") or "").strip()
    if not email or "@" not in email:
        raise MailNestClientError("MailNest 购买邮箱响应缺少有效 email")
    account = MailNestAccount(email=email, project_code=project_code)
    _CONTEXT_CACHE[_cache_key(email)] = account
    logger.info("[MailNest] 已获取临时邮箱: %s project_code=%s", email, project_code)
    return account


def get_email() -> str:
    """兼容旧入口：返回新领取的邮箱地址。"""
    return pick_account().email


def get_account_context(email: str) -> MailNestAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info("[MailNest] 已释放临时邮箱: %s（status=%s, note=%s）", email, status, note or "")


def _get_mails(email: str):
    return _request("POST", "/api/v1/email/receive", json={"email": email})


def _timestamp(item: dict) -> float | None:
    for key in ("timestamp", "created_at", "create_time", "received_at", "date"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _otp_item(item: dict) -> dict:
    return {
        "id": item.get("id") or item.get("mail_id"),
        "from": item.get("from") or item.get("from_address") or item.get("sender") or "",
        "subject": item.get("subject") or item.get("title") or "",
        "text": item.get("text") or item.get("content") or item.get("body") or "",
        "html": item.get("html") or item.get("html_content") or "",
    }


def fetch_otp_once(
    email: str,
    after_ts: float | None,
    state: MailNestProbeState,
    settle_seconds: int | None = None,
) -> OTPProbeResult:
    """单次 MailNest 取码 probe；不 sleep、不维护总 deadline。"""
    target = str(email or "").strip()
    if not target:
        raise MailNestClientError("MailNest 取码缺少邮箱地址")

    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    mails = _get_mails(target)
    if not isinstance(mails, list):
        raise MailNestClientError("MailNest 收件箱响应不是数组")
    for mail in sorted(mails, key=lambda item: _timestamp(item) or float("-inf"), reverse=True):
        if not isinstance(mail, dict):
            continue
        message_time = _timestamp(mail)
        if after_ts is not None and message_time is not None and message_time < after_ts - 30:
            continue
        otp = str(mail.get("code_match") or "").strip()
        item = _otp_item(mail)
        if not otp:
            if not looks_like_openai_email(item):
                continue
            otp = extract_otp(item) or ""
        if not otp:
            continue
        candidate_time = float("-inf") if message_time is None else message_time
        if state.best_otp is None or candidate_time > state.best_timestamp or (candidate_time == state.best_timestamp and otp != state.best_otp):
            state.best_otp = otp
            state.best_timestamp = candidate_time
            state.settle_until = time.monotonic() + settle
            logger.info("[MailNest] 锁定 OTP 候选，等待 %ss 确认", settle)
    now = time.monotonic()
    if state.best_otp and state.settle_until is not None and now >= state.settle_until:
        return OTPProbeResult.completed(state.best_otp, state)
    return OTPProbeResult.candidate(state, state.settle_until) if state.best_otp else OTPProbeResult.pending(state)


def fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None) -> str:
    """按统一策略轮询 MailNest，返回领取时间后最新的 OpenAI 六位验证码。"""
    target = str(email or "").strip()
    if not target:
        raise MailNestClientError("MailNest 取码缺少邮箱地址")
    state = MailNestProbeState()
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), _email_cfg.OTP_MAX_WAIT)
    return wait_for_otp_with_policy(provider="mailnest", email=target, initial_after_ts=after_ts if after_ts is not None else time.time(), fetch=lambda stamp: fetch_otp_once(target, stamp, state, settle_seconds), retrigger=lambda: time.time(), wait_timeout=timeout, poll_interval=poll_interval or _email_cfg.OTP_POLL_INTERVAL, retry_count=0)
