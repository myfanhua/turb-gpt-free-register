# -*- coding: utf-8 -*-
"""ChatGPT 前端 bootstrap 预热链路。

根据 docs/protocol_fingerprint_har_analysis.md / protocol_har_summary.json
补齐与真实 Web 首屏更接近的 backend-anon / backend-api 初始化请求。该模块只做
可失败的预热：任何单个接口异常都会记录并继续，不打断注册主流程。
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

from core.session import BrowserSession
from core.sentinel import generate_requirements_token

logger = logging.getLogger(__name__)

_ANON_BASE = "https://chatgpt.com/backend-anon"
_API_BASE = "https://chatgpt.com/backend-api"


def _json_post(session: BrowserSession, url: str, payload: dict, referer: str, headers: dict | None = None):
    h = headers or session.get_chatgpt_headers(referer=referer)
    return session.post(url, headers=h, data=json.dumps(payload, separators=(",", ":")))


def _safe_request(label: str, fn, *, strict: bool = False):
    try:
        resp = fn()
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {(getattr(resp, 'text', '') or '')[:180]}")
        return resp
    except Exception as exc:
        if strict:
            raise
        logger.debug("[Bootstrap] %s 跳过/失败：%s: %s", label, type(exc).__name__, str(exc)[:180])
        return None


def _system_hint_paths(modes: Iterable[str], base: str) -> list[str]:
    return [f"{base}/system_hints?mode={mode}" for mode in modes]


def _chat_requirements_prepare(session: BrowserSession, base: str, referer: str, *, strict: bool = False):
    """POST sentinel/chat-requirements/prepare，p 字段与会话画像一致。"""
    sid = getattr(session, "sentinel_sid", session.device_id)
    p = generate_requirements_token(sid, profile=getattr(session, "browser_profile", None))
    return _safe_request(
        f"{base}/sentinel/chat-requirements/prepare",
        lambda: _json_post(
            session,
            f"{base}/sentinel/chat-requirements/prepare",
            {"p": p},
            referer=referer,
        ),
        strict=strict,
    )


def _maybe_chat_requirements_finalize(session: BrowserSession, base: str, referer: str, prepare_resp, *, strict: bool = False):
    """
    HAR 中 finalize 需要 prepare_token/proofofwork/turnstile。不同版本返回结构会变，
    只有在 prepare 响应明确给到可用字段时才提交，避免构造半截 challenge。
    """
    if prepare_resp is None:
        return None
    try:
        data = prepare_resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    prepare_token = data.get("prepare_token") or data.get("token") or data.get("c")
    if not prepare_token:
        return None
    payload = {"prepare_token": prepare_token}
    for key in ("proofofwork", "turnstile"):
        value = data.get(key)
        if value:
            payload[key] = value
    return _safe_request(
        f"{base}/sentinel/chat-requirements/finalize",
        lambda: _json_post(session, f"{base}/sentinel/chat-requirements/finalize", payload, referer=referer),
        strict=strict,
    )


def anonymous_bootstrap(session: BrowserSession, *, strict: bool = False) -> None:
    """注册前匿名态 ChatGPT 首页/模型预热。"""
    referer = "https://chatgpt.com/"
    tz = session.js_timezone_offset_min()
    logger.info("[Bootstrap] 匿名态 ChatGPT 预热开始")
    _safe_request("anon accounts/check", lambda: session.get(
        f"{_ANON_BASE}/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
        headers=session.get_chatgpt_headers(referer=referer),
    ), strict=strict)
    _safe_request("anon me", lambda: session.get(f"{_ANON_BASE}/me", headers=session.get_chatgpt_headers(referer=referer)), strict=strict)
    prep = _chat_requirements_prepare(session, _ANON_BASE, referer, strict=strict)
    for url in [
        *_system_hint_paths(("custom_agents", "connectors", "basic"), _ANON_BASE),
        f"{_ANON_BASE}/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true",
    ]:
        _safe_request(url, lambda u=url: session.get(u, headers=session.get_chatgpt_headers(referer=referer)), strict=strict)
    _safe_request("anon conversation/init", lambda: _json_post(session, f"{_ANON_BASE}/conversation/init", {
        "requested_default_model": None,
        "conversation_id": None,
        "timezone_offset_min": tz,
        "conversation_origin": None,
    }, referer=referer), strict=strict)
    _maybe_chat_requirements_finalize(session, _ANON_BASE, referer, prep, strict=strict)
    logger.info("[Bootstrap] 匿名态 ChatGPT 预热完成")


def authenticated_bootstrap(session: BrowserSession, access_token: str | None = None, *, strict: bool = False) -> None:
    """登录态 ChatGPT bootstrap，access_token 存在时补 Authorization。"""
    referer = "https://chatgpt.com/"
    tz = session.js_timezone_offset_min()

    def headers():
        h = session.get_chatgpt_headers(referer=referer)
        if access_token:
            h["authorization"] = access_token if access_token.lower().startswith("bearer ") else f"Bearer {access_token}"
        return h

    logger.info("[Bootstrap] 登录态 ChatGPT 预热开始")
    for path in [
        "/accounts/optimized/check",
        "/user_granular_consent",
        "/me",
        f"/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
        "/settings/user",
    ]:
        _safe_request(f"auth {path}", lambda p=path: session.get(f"{_API_BASE}{p}", headers=headers()), strict=strict)
    prep = _chat_requirements_prepare(session, _API_BASE, referer, strict=strict)
    for url in [
        *_system_hint_paths(("custom_agents", "connectors", "basic"), _API_BASE),
        f"{_API_BASE}/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true",
    ]:
        _safe_request(url, lambda u=url: session.get(u, headers=headers()), strict=strict)
    _safe_request("auth conversation/init", lambda: _json_post(session, f"{_API_BASE}/conversation/init", {
        "requested_default_model": None,
        "conversation_id": None,
        "timezone_offset_min": tz,
        "conversation_origin": None,
    }, referer=referer, headers=headers()), strict=strict)
    _maybe_chat_requirements_finalize(session, _API_BASE, referer, prep, strict=strict)
    for path in [
        "/conversations?offset=0&limit=28&order=updated",
        "/client/strings",
        "/settings/user",
    ]:
        _safe_request(f"auth {path}", lambda p=path: session.get(f"{_API_BASE}{p}", headers=headers()), strict=strict)
    logger.info("[Bootstrap] 登录态 ChatGPT 预热完成")
