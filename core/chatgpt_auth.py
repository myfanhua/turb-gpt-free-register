# -*- coding: utf-8 -*-
"""
ChatGPT Auth 模块
处理 chatgpt.com 域名下的认证请求（步骤1-3）
"""
import json
import logging
from urllib.parse import urlencode, urlparse, parse_qs

from core.session import BrowserSession
from config import (
    OPENAI_CLIENT_ID, OPENAI_SCOPE, OPENAI_AUDIENCE, OPENAI_REDIRECT_URI
)

logger = logging.getLogger(__name__)

# 2026-09-14 Roxy 成功样本：signin 不再主动携带 passkey capabilities；
# authorize 使用两个 ccaps，并明确返回 ChatGPT 首页。
_CC_CAPS = "login_methods chatgpt_login_finalizer_v1"


def _ensure_authorize_context(authorize_url: str, session: BrowserSession, email: str) -> str:
    """
    对 NextAuth 返回的 authorize URL 做最后兜底：确保当前前端默认
    login_or_signup 链路的上下文参数没有在重定向生成阶段丢失。
    """
    try:
        parsed = urlparse(authorize_url)
        if parsed.scheme != "https" or parsed.hostname != "auth.openai.com":
            return authorize_url
        params = parse_qs(parsed.query, keep_blank_values=True)
        # 只补缺失上下文，不能删改服务端返回的能力列表或本地化选择。
        changed = False
        language_getter = getattr(session, "navigator_language", None)
        if callable(language_getter):
            ui_locale = str(language_getter() or "en-US")
        else:
            # 兼容精简会话/旧任务对象，同时避免因为缺少一个辅助方法而把
            # ext-oai-did 等整个 authorize 上下文静默丢掉。
            profile = getattr(session, "browser_profile", {}) or {}
            ui_locale = str(profile.get("navigator_language") or "en-US")
        required = {
            "ext-oai-did": session.device_id,
            "auth_session_logging_id": session.auth_session_logging_id,
            "screen_hint": "login_or_signup",
            "login_hint": email,
            "ui_locales": ui_locale,
            "ccaps": _CC_CAPS,
            "auth_return_target_category": "chatgpt_home",
        }
        for key, value in required.items():
            if value and key not in params:
                params[key] = [value]
                changed = True
        if not changed:
            return authorize_url
        logger.info(
            "[步骤3] authorize 上下文已对齐：ui_locales=%s oai-did=%s",
            ui_locale,
            str(session.device_id)[:12] + "...",
        )
        return parsed._replace(query=urlencode(params, doseq=True)).geturl()
    except Exception:
        return authorize_url


def get_providers(session: BrowserSession) -> dict:
    """
    步骤1: 获取 OAuth Providers 列表。
    GET https://chatgpt.com/api/auth/providers

    验证与 chatgpt.com 的连接是否正常，并获取可用的 OAuth 提供商。

    Returns:
        providers 字典，例如:
        {
            "openai": {
                "id": "openai",
                "name": "openai",
                "type": "oauth",
                "signinUrl": "https://chatgpt.com/api/auth/signin/openai",
                "callbackUrl": "https://chatgpt.com/api/auth/callback/openai"
            },
            ...
        }
    """
    url = "https://chatgpt.com/api/auth/providers"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")

    logger.info("[步骤1] 获取 OAuth Providers...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()

    data = resp.json()
    logger.info(f"[步骤1] 成功获取 {len(data)} 个 providers: {list(data.keys())}")
    return data


def get_csrf_token(session: BrowserSession) -> str:
    """
    步骤2: 获取 CSRF Token。
    GET https://chatgpt.com/api/auth/csrf

    CSRF token 将在后续 signin 请求中使用。

    Returns:
        csrfToken 字符串
    """
    url = "https://chatgpt.com/api/auth/csrf"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")

    logger.info("[步骤2] 获取 CSRF Token...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()

    data = resp.json()
    csrf_token = data.get("csrfToken") if isinstance(data, dict) else None
    if resp.status_code != 200 or not isinstance(csrf_token, str) or not csrf_token.strip():
        raise RuntimeError("CSRF Token 获取失败：响应缺少有效 csrfToken")
    logger.info(f"[步骤2] 获取 CSRF Token 成功: {csrf_token[:20]}...")
    return csrf_token


def probe_auth_session(session: BrowserSession) -> dict:
    """按 Web 登录页顺序在 providers 之后读取一次匿名 NextAuth session。"""
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")
    logger.info("[步骤1.5] 读取匿名 Auth Session...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def signin_openai(session: BrowserSession, csrf_token: str, email: str) -> str:
    """
    步骤3: 发起 OAuth Signin 请求。
    POST https://chatgpt.com/api/auth/signin/openai

    构造 OAuth 授权参数，获取 authorize URL。

    Args:
        session: 浏览器会话
        csrf_token: 从步骤2获取的 CSRF token
        email: 注册邮箱

    Returns:
        authorize_url: auth.openai.com 的授权 URL
    """
    sync = getattr(session, "sync_device_id_from_cookies", None)
    if callable(sync):
        sync("chatgpt.com")
    # 构造 URL 查询参数
    query_params = {
        "prompt": "login",
        "ext-oai-did": session.device_id,
        "auth_session_logging_id": session.auth_session_logging_id,
        "screen_hint": "login_or_signup",
        "login_hint": email,
    }
    url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query_params)

    # 构造请求头
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")
    headers["content-type"] = "application/x-www-form-urlencoded"

    # 构造请求体
    body = urlencode({
        "callbackUrl": "/",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info(f"[步骤3] 发起 OAuth Signin 请求, 邮箱: {email}")
    resp = session.post(url, headers=headers, data=body)
    resp.raise_for_status()

    data = resp.json()
    authorize_url = data.get("url", "")

    if not authorize_url:
        raise ValueError(f"[步骤3] 未获取到 authorize URL, 响应: {data}")

    from core.protocol_registration import normalize_auth_url
    authorize_url = normalize_auth_url(authorize_url)
    if urlparse(authorize_url).hostname != "auth.openai.com":
        raise RuntimeError("signin 响应未指向 Auth 授权域名")

    authorize_url = _ensure_authorize_context(authorize_url, session, email)
    logger.info("[步骤3] 获取 authorize URL 成功，已确认 login_or_signup/oai-did 上下文")
    logger.debug(f"[步骤3] URL: {authorize_url[:160]}...")
    return authorize_url
