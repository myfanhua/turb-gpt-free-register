# -*- coding: utf-8 -*-
"""GPTMail 临时邮箱客户端。"""
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

BASE_URL = "https://mail.chatgpt.org.uk"
REQUEST_TIMEOUT = 20


class GPTMailError(RuntimeError):
    """GPTMail 服务请求或邮箱取码失败。"""


@dataclass
class GPTMailAccount:
    email: str


@dataclass
class GPTMailProbeState:
    best_otp: str | None = None
    best_timestamp: float = float("-inf")
    settle_until: float | None = None


_CONTEXT_CACHE: dict[str, GPTMailAccount] = {}


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _headers() -> dict[str, str]:
    api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
    if not api_key:
        raise GPTMailError(
            "GPTMail API Key 未配置，请填写 GPTMail API Key（WebUI「配置 → 邮箱 / OTP」）。"
        )
    return {"Accept": "application/json", "X-API-Key": api_key}


def _get(path: str, params: dict | None = None) -> dict:
    """调用 GPTMail GET 接口，返回成功响应中的 data 对象。"""
    try:
        response = requests.get(
            BASE_URL + path,
            headers=_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GPTMailError(f"GPTMail 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise GPTMailError(f"GPTMail 响应不是 JSON ({path}): HTTP {response.status_code}") from exc

    if response.status_code != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
        message = payload.get("error") if isinstance(payload, dict) else ""
        if not message:
            message = getattr(response, "text", "")[:160]
        raise GPTMailError(f"GPTMail 请求失败 ({path}): HTTP {response.status_code}; {message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise GPTMailError(f"GPTMail 响应缺少对象 data ({path})")
    return data


def pick_account() -> GPTMailAccount:
    """生成并缓存一个新的 GPTMail 随机邮箱地址。"""
    data = _get("/api/generate-email")
    email = str(data.get("email") or "").strip()
    if not email or "@" not in email:
        raise GPTMailError("GPTMail 生成邮箱响应缺少有效 email")
    account = GPTMailAccount(email=email)
    _CONTEXT_CACHE[_cache_key(email)] = account
    logger.info("[GPTMail] 已生成临时邮箱: %s", email)
    return account


def get_account_context(email: str) -> GPTMailAccount | None:
    """返回当前进程已生成的 GPTMail 邮箱上下文。"""
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """GPTMail 地址无需入池，任务结束时只清理本进程上下文。"""
    _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info("[GPTMail] 已释放临时邮箱: %s（status=%s, note=%s）", email, status, note or "")


def _timestamp(item: dict) -> float | None:
    raw = item.get("timestamp")
    try:
        if raw is not None:
            return float(raw)
    except (TypeError, ValueError):
        pass

    created_at = str(item.get("created_at") or "").strip()
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _otp_item(item: dict) -> dict:
    """把 GPTMail 字段映射为项目通用 OTP 工具支持的字段。"""
    return {
        "id": item.get("id"),
        "from": item.get("from_address") or item.get("from") or "",
        "subject": item.get("subject") or "",
        "text": item.get("content") or item.get("text") or "",
        "html": item.get("html_content") or item.get("html") or "",
    }


def fetch_otp_once(
    email: str,
    after_ts: float | None,
    state: GPTMailProbeState,
    settle_seconds: int | None = None,
) -> OTPProbeResult:
    """单次 GPTMail 取码 probe；不 sleep、不维护总 deadline。"""
    target = str(email or "").strip()
    if not target:
        raise GPTMailError("GPTMail 取码缺少邮箱地址")

    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    data = _get("/api/emails", params={"email": target})
    emails = data.get("emails")
    if not isinstance(emails, list):
        raise GPTMailError("GPTMail 收件箱响应缺少 emails 数组")

    sortable = sorted(emails, key=lambda item: _timestamp(item) or float("-inf"), reverse=True)
    for summary in sortable:
        if not isinstance(summary, dict):
            continue
        message_time = _timestamp(summary)
        if after_ts is not None and message_time is not None and message_time < after_ts - 30:
            continue
        summary_item = _otp_item(summary)
        if not looks_like_openai_email(summary_item):
            continue
        message_id = str(summary.get("id") or "").strip()
        if not message_id:
            continue
        detail = _get(f"/api/email/{message_id}")
        detail_item = _otp_item(detail)
        if not looks_like_openai_email(detail_item):
            continue
        otp = extract_otp(detail_item)
        if not otp:
            continue
        candidate_time = _timestamp(detail)
        candidate_time = message_time if candidate_time is None else candidate_time
        candidate_time = float("-inf") if candidate_time is None else candidate_time
        if state.best_otp is None or candidate_time > state.best_timestamp or (candidate_time == state.best_timestamp and otp != state.best_otp):
            state.best_otp = otp
            state.best_timestamp = candidate_time
            state.settle_until = time.monotonic() + settle
            logger.info("[GPTMail] 锁定 OTP 候选，等待 %ss 确认", settle)

    now = time.monotonic()
    if state.best_otp and state.settle_until is not None and now >= state.settle_until:
        return OTPProbeResult.completed(state.best_otp, state)
    return OTPProbeResult.candidate(state, state.settle_until) if state.best_otp else OTPProbeResult.pending(state)


def fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None) -> str:
    """按统一策略轮询 GPTMail，返回领取时间后最新的 OpenAI 六位验证码。"""
    target = str(email or "").strip()
    if not target:
        raise GPTMailError("GPTMail 取码缺少邮箱地址")
    state = GPTMailProbeState()
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), _email_cfg.OTP_MAX_WAIT)
    return wait_for_otp_with_policy(provider="gptmail", email=target, initial_after_ts=after_ts if after_ts is not None else time.time(), fetch=lambda stamp: fetch_otp_once(target, stamp, state, settle_seconds), retrigger=lambda: time.time(), wait_timeout=timeout, poll_interval=poll_interval or _email_cfg.OTP_POLL_INTERVAL, retry_count=0)
