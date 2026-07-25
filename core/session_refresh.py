# -*- coding: utf-8 -*-
"""ChatGPT 已保存 Web 会话的受控续期。

此模块只使用本地已保存的会话 Cookie 请求官方 session 端点；不模拟挑战、
不生成 OAuth 凭证，也不记录任何认证材料。
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import requests

from core import db

SESSION_ENDPOINT = "https://chatgpt.com/api/auth/session"
_SESSION_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
}


def _extra(account: dict) -> dict:
    value = account.get("extra_json") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def _jwt_exp(token: str) -> int | None:
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _iso_utc(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _cookie_dict(raw: Any) -> dict[str, str]:
    """把已保存的 Cookie 表示归一化为 requests 可用的键值对。"""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if k and v is not None}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                out[str(item["name"])] = str(item["value"])
        return out
    return {}


def token_status(account: dict, *, now: datetime | None = None) -> dict:
    """返回可公开显示的 Token/续期状态，绝不包含凭证内容。"""
    artifacts = _extra(account).get("auth_artifacts") or {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    access = str(account.get("access_token") or artifacts.get("access_token") or "")
    exp = _jwt_exp(access)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    oauth_refreshable = bool(str(artifacts.get("refresh_token") or account.get("oauth_refresh_token") or "").strip())
    cookies = _cookie_dict(artifacts.get("cookies"))
    session_saved = isinstance(artifacts.get("session"), dict)
    session_renewal_possible = bool(cookies)
    if oauth_refreshable:
        refresh_mode, refresh_label = "oauth_refresh", "OAuth 可刷新"
    elif session_renewal_possible:
        refresh_mode, refresh_label = "session_renewal", "可尝试会话续期"
    else:
        refresh_mode, refresh_label = "none", "无可用刷新材料"
    return {
        "access_token_present": bool(access.strip()),
        "expires_at": _iso_utc(exp),
        "expires_epoch": exp,
        "expired": bool(exp is not None and current.timestamp() >= exp),
        "oauth_refreshable": oauth_refreshable,
        "session_renewal_possible": session_renewal_possible,
        "session_saved": session_saved,
        "saved_cookie_count": len(cookies),
        "refresh_mode": refresh_mode,
        "refresh_label": refresh_label,
        "snapshot_only": not oauth_refreshable,
    }


def _failure(status: dict, *, reason: str, message: str, http_status: int | None = None) -> dict:
    return {"ok": False, "refreshed": False, "reason": reason, "message": message, "http_status": http_status, "status": status}


def refresh_account_session(account_id: int, *, timeout: int = 15) -> dict:
    """显式尝试用已保存 Cookie 调用合法 session endpoint，不绕过挑战。"""
    account = db.get_account(account_id)
    if not account:
        return _failure({}, reason="account_not_found", message="账号不存在")
    before = token_status(account)
    artifacts = _extra(account).get("auth_artifacts") or {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    cookies = _cookie_dict(artifacts.get("cookies"))
    if not cookies:
        return _failure(before, reason="missing_session_cookies", message="未保存可用会话 Cookie，无法尝试会话续期")

    session = requests.Session()
    session.cookies.update(cookies)
    try:
        response = session.get(SESSION_ENDPOINT, headers=_SESSION_HEADERS, timeout=timeout)
    except requests.RequestException:
        return _failure(before, reason="network_error", message="会话端点请求失败；未修改本地认证资产")

    body = (getattr(response, "text", "") or "").lower()
    http_status = int(getattr(response, "status_code", 0) or 0)
    if http_status in {401, 403} or any(marker in body for marker in ("turnstile", "cloudflare", "cf-chl", "attention required")):
        return _failure(before, reason="browser_verification_required", message="会话续期被登录验证或 Cloudflare/Turnstile 拦截；请在真实浏览器完成验证，程序未尝试绕过", http_status=http_status)
    if http_status < 200 or http_status >= 300:
        return _failure(before, reason="session_http_error", message="会话端点未成功响应；未修改本地认证资产", http_status=http_status)
    try:
        session_data = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        return _failure(before, reason="invalid_session_response", message="会话端点响应无法解析；未修改本地认证资产", http_status=http_status)
    if not isinstance(session_data, dict) or not str(session_data.get("accessToken") or "").strip():
        return _failure(before, reason="missing_access_token", message="会话端点未返回 accessToken；登录态可能已失效，未修改本地认证资产", http_status=http_status)

    new_access = str(session_data["accessToken"]).strip()
    refreshed_cookies = session.cookies.get_dict() or cookies
    after = token_status({"access_token": new_access, "extra_json": {"auth_artifacts": {"session": session_data, "cookies": refreshed_cookies, "refresh_token": artifacts.get("refresh_token") or ""}}})
    if not db.update_account_auth_session(
        account_id,
        access_token=new_access,
        session_data=session_data,
        cookies=refreshed_cookies,
        token_expires_at=after.get("expires_at"),
    ):
        return _failure(before, reason="account_not_found", message="账号在续期过程中不存在，未保存新登录态", http_status=http_status)
    return {"ok": True, "refreshed": True, "reason": "session_renewed", "message": "已通过已保存会话更新本地登录态", "http_status": http_status, "status": after}
