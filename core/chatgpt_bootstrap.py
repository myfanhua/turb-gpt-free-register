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
from core.http_diagnostics import response_summary

logger = logging.getLogger(__name__)

_ANON_BASE = "https://chatgpt.com/backend-anon"
_API_BASE = "https://chatgpt.com/backend-api"

_DIAGNOSTIC_KEY_PARTS = (
    "eligib", "trial", "offer", "promo", "plan", "subscription",
    "country", "region", "experiment", "variant", "reason", "code",
)


def _diagnostic_response_summary(resp, limit: int = 1400) -> str:
    """提取资格相关响应字段，不把 access token、Cookie 或完整用户资料写入日志。"""
    if resp is None:
        return "无响应"
    status = int(getattr(resp, "status_code", 0) or 0)
    try:
        payload = resp.json()
    except Exception:
        text = str(getattr(resp, "text", "") or "").replace("\n", " ")[:240]
        return f"status={status} body={text or '<empty>'}"

    selected: dict[str, object] = {}

    def walk(value, path="", depth=0):
        if depth > 6 or len(selected) >= 60:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                lowered = key_text.lower()
                if any(part in lowered for part in _DIAGNOSTIC_KEY_PARTS):
                    if child is None or isinstance(child, (str, int, float, bool)):
                        selected[child_path] = child
                    elif isinstance(child, list) and len(child) <= 12:
                        selected[child_path] = child
                walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:20]):
                walk(child, f"{path}[{index}]", depth + 1)

    walk(payload)
    # eligibility 响应有时本身就是很小的扁平对象；这种情况下保留全部非敏感标量。
    if not selected and isinstance(payload, dict) and len(payload) <= 20:
        blocked = ("token", "email", "name", "id", "cookie", "secret")
        selected = {
            str(k): v for k, v in payload.items()
            if not any(part in str(k).lower() for part in blocked)
            and (v is None or isinstance(v, (str, int, float, bool)))
        }
    encoded = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
    return f"status={status} fields={encoded[:limit]}"


def _json_post(session: BrowserSession, url: str, payload: dict, referer: str, headers: dict | None = None):
    h = headers or session.get_chatgpt_headers(referer=referer)
    return session.post(url, headers=h, data=json.dumps(payload, separators=(",", ":")))


def _safe_request(label: str, fn, *, strict: bool = False):
    try:
        resp = fn()
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            logger.warning("[Bootstrap] %s 请求失败：%s", label, response_summary(resp))
            raise RuntimeError(f"HTTP {status}")
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
    """注册前匿名态登录页初始化。

    仅保留 accounts/check、CES settings 和 me；不再请求匿名地区定价配置。
    不额外发送匿名 chat-requirements/models/conversation/init 请求。
    """
    referer = "https://chatgpt.com/auth/login"
    tz = session.js_timezone_offset_min()
    logger.info("[Bootstrap] 匿名态 ChatGPT 预热开始")
    _safe_request("anon accounts/check", lambda: session.get(
        f"{_ANON_BASE}/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
        headers=session.get_chatgpt_headers(referer=referer),
    ), strict=strict)
    _safe_request("CES settings", lambda: session.get(
        "https://chatgpt.com/ces/v1/projects/oai/settings",
        headers=session.get_nextauth_headers(referer=referer),
    ), strict=strict)
    _safe_request("anon me", lambda: session.get(f"{_ANON_BASE}/me", headers=session.get_chatgpt_headers(referer=referer)), strict=strict)
    if not strict:
        # best-effort 预热中的可选接口即使返回 403，也不能让本地熔断器阻断
        # 后续正式的 NextAuth 注册链路。
        reset = getattr(session, "reset_circuit_breaker", None)
        if callable(reset):
            reset()
    log_cookies = getattr(session, "log_cookie_names", None)
    if callable(log_cookies):
        log_cookies("anonymous_bootstrap_complete")
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
    diagnostic_paths = {
        "/accounts/optimized/check",
        "/me",
        f"/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
    }
    for path in [
        "/user_granular_consent",
        "/settings/is_adult",
        "/accounts/optimized/check",
        "/me",
        f"/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
        "/settings/user",
    ]:
        resp = _safe_request(
            f"auth {path}",
            lambda p=path: session.get(f"{_API_BASE}{p}", headers=headers()),
            strict=strict,
        )
        if path in diagnostic_paths:
            logger.info("[资格诊断] endpoint=%s %s", path, _diagnostic_response_summary(resp))
    prep = _chat_requirements_prepare(session, _API_BASE, referer, strict=strict)
    for url in [
        f"{_API_BASE}/system_hints?mode=basic",
        f"{_API_BASE}/system_hints?mode=plugins&suggestions=true",
        f"{_API_BASE}/system_hints?mode=custom_agents",
        f"{_API_BASE}/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true",
    ]:
        _safe_request(url, lambda u=url: session.get(u, headers=headers()), strict=strict)
    _maybe_chat_requirements_finalize(session, _API_BASE, referer, prep, strict=strict)
    for path in [
        "/calpico/chatgpt/rooms/summary?limit=10&include_pinned=true&include_magic_link=false",
        "/pins",
        "/conversations?offset=0&limit=28&order=updated&is_archived=false&is_starred=false",
        "/aip/first-party/eligibility",
    ]:
        resp = _safe_request(
            f"auth {path}",
            lambda p=path: session.get(f"{_API_BASE}{p}", headers=headers()),
            strict=strict,
        )
        if path == "/aip/first-party/eligibility":
            logger.info("[资格诊断] endpoint=%s %s", path, _diagnostic_response_summary(resp))
    log_cookies = getattr(session, "log_cookie_names", None)
    if callable(log_cookies):
        log_cookies("authenticated_bootstrap_complete")
    logger.info("[Bootstrap] 登录态 ChatGPT 可选预热流程结束（允许部分失败）")
