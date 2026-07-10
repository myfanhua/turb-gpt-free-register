# -*- coding: utf-8 -*-
"""
ChatGPT Auth 模块
处理 chatgpt.com 域名下的认证请求（步骤1-3）
"""
import json
import logging
from urllib.parse import urlencode, quote

from core.session import BrowserSession
from config import (
    OPENAI_CLIENT_ID, OPENAI_SCOPE, OPENAI_AUDIENCE, OPENAI_REDIRECT_URI
)

logger = logging.getLogger(__name__)


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
    headers = session.get_chatgpt_headers()

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
    headers = session.get_chatgpt_headers()

    logger.info("[步骤2] 获取 CSRF Token...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()

    data = resp.json()
    csrf_token = data.get("csrfToken", "")
    logger.info(f"[步骤2] 获取 CSRF Token 成功: {csrf_token[:20]}...")
    return csrf_token


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
    # 构造 URL 查询参数
    query_params = {
        "prompt": "login",
        "ext-oai-did": session.device_id,
        "auth_session_logging_id": session.auth_session_logging_id,
        "ext-passkey-client-capabilities": "1111",
        "screen_hint": "login_or_signup",
        "login_hint": email,
    }
    url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query_params)

    # 构造请求头
    headers = session.get_chatgpt_headers()
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    # 构造请求体
    body = urlencode({
        "callbackUrl": "https://chatgpt.com/login",
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

    logger.info(f"[步骤3] 获取 authorize URL 成功")
    logger.debug(f"[步骤3] URL: {authorize_url[:100]}...")
    return authorize_url
