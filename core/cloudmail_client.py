# -*- coding: utf-8 -*-
"""CloudMail/Cloud Mail API 邮箱客户端。"""
from __future__ import annotations

import logging
import random
import re
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email
from core.otp_wait_policy import OTPProbeResult, resolve_wait_timeout, wait_for_otp_with_policy

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20


class CloudMailError(RuntimeError):
    """CloudMail/Cloud Mail 服务请求或邮箱取码失败。"""


@dataclass
class CloudMailAccount:
    email: str
    domain: str


@dataclass
class CloudMailProbeState:
    best_otp: str | None = None
    best_timestamp: float = float("-inf")
    settle_until: float | None = None


_CONTEXT_CACHE: dict[str, CloudMailAccount] = {}
_DOMAIN_CACHE: tuple[float, list[str]] | None = None
DOMAIN_CACHE_TTL = 300


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url(value: str | None = None) -> str:
    base = str(value if value is not None else getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise CloudMailError("CloudMail API 地址未配置，请填写 CLOUDMAIL_API_BASE（例如 https://你的worker自定义域）。")
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    return base


def _token() -> str:
    token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
    if not token:
        raise CloudMailError("CloudMail Authorization Token 未配置，请填写 CLOUDMAIL_AUTH_TOKEN。")
    return token


def _token_path_candidates(path: str | None = None) -> list[str]:
    first = str(path or getattr(_email_cfg, "CLOUDMAIL_TOKEN_PATH", "") or "/api/public/genToken").strip()
    out = []
    for item in (first, "/api/public/genToken"):
        if not item:
            continue
        if not item.startswith("/"):
            item = "/" + item
        if item not in out:
            out.append(item)
    return out


def _extract_token(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    candidates = []
    if isinstance(payload, dict):
        candidates.extend([payload.get("token"), payload.get("accessToken"), payload.get("access_token")])
    if isinstance(data, dict):
        candidates.extend([data.get("token"), data.get("accessToken"), data.get("access_token"), data.get("authorization")])
    if isinstance(data, str):
        candidates.append(data)
    for item in candidates:
        token = str(item or "").strip()
        if token:
            return token
    raise CloudMailError(f"CloudMail 生成 Token 成功但响应中未找到 token: {str(payload)[:200]}")


def gen_token(email: str | None = None, password: str | None = None, path: str | None = None, base_url: str | None = None) -> str:
    """用 CloudMail 管理员邮箱/密码生成 Authorization Token。"""
    email = str(email if email is not None else getattr(_email_cfg, "CLOUDMAIL_ADMIN_EMAIL", "") or "").strip()
    password = str(password if password is not None else getattr(_email_cfg, "CLOUDMAIL_PASSWORD", "") or "").strip()
    if not email or not password:
        raise CloudMailError("CloudMail 管理员邮箱或密码为空")

    last_error = ""
    for token_path in _token_path_candidates(path):
        url = _base_url(base_url) + token_path
        try:
            resp = requests.post(
                url,
                json={"email": email, "password": password},
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            payload = resp.json()
        except Exception as exc:
            last_error = f"{token_path}: {type(exc).__name__}: {exc}"
            continue
        code = payload.get("code") if isinstance(payload, dict) else None
        if resp.status_code < 400 and code in (200, "200", None):
            try:
                token = _extract_token(payload)
                logger.info("[CloudMail] 生成 token 成功：path=%s", token_path)
                return token
            except CloudMailError as exc:
                last_error = str(exc)
                continue
        message = payload.get("message") if isinstance(payload, dict) else ""
        last_error = f"{token_path}: HTTP {resp.status_code}; code={code}; {message or str(payload)[:160]}"
    raise CloudMailError(f"CloudMail 生成 Token 失败: {last_error}")


# 兼容旧内部调用名。
def login(username: str | None = None, password: str | None = None, path: str | None = None, base_url: str | None = None) -> str:
    return gen_token(email=username, password=password, path=path, base_url=base_url)


def _domains() -> list[str]:
    raw = getattr(_email_cfg, "CLOUDMAIL_DOMAINS", []) or []
    out = _normalize_domains(raw)
    if not out:
        out = fetch_domains()
    if not out:
        raise CloudMailError("CloudMail 邮箱域名列表为空，且未能从平台自动获取。")
    return out


def _request(path: str, body: dict | None = None):
    url = _base_url() + path
    try:
        resp = requests.post(
            url,
            json=body or {},
            headers={"Authorization": _token(), "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise CloudMailError(f"CloudMail 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise CloudMailError(f"CloudMail 响应不是 JSON ({path}): HTTP {resp.status_code}") from exc

    code = payload.get("code") if isinstance(payload, dict) else None
    if resp.status_code >= 400 or code not in (200, "200"):
        message = payload.get("message") if isinstance(payload, dict) else ""
        raise CloudMailError(f"CloudMail 请求失败 ({path}): HTTP {resp.status_code}; code={code}; {message or str(payload)[:200]}")
    return payload.get("data")


def _request_raw(method: str, path: str, *, body: dict | None = None, token: str | None = None):
    url = _base_url() + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = token
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            resp = requests.post(url, json=body or {}, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise CloudMailError(f"CloudMail 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise CloudMailError(f"CloudMail 响应不是 JSON ({path}): HTTP {resp.status_code}") from exc

    code = payload.get("code") if isinstance(payload, dict) else None
    if resp.status_code >= 400 or code not in (200, "200", None):
        message = payload.get("message") if isinstance(payload, dict) else ""
        raise CloudMailError(f"CloudMail 请求失败 ({path}): HTTP {resp.status_code}; code={code}; {message or str(payload)[:200]}")
    return payload


def _normalize_domains(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace(";", "\n").replace(",", "\n").splitlines()
    elif isinstance(raw, dict):
        parts = []
        for key in ("domain", "name", "value", "emailDomain"):
            if raw.get(key):
                parts.append(raw.get(key))
    else:
        try:
            parts = list(raw)
        except TypeError:
            parts = [raw]
    out = []
    for item in parts:
        if isinstance(item, dict):
            candidates = [item.get(k) for k in ("domain", "name", "value", "emailDomain")]
        else:
            candidates = [item]
        for candidate in candidates:
            domain = str(candidate or "").strip().lower().lstrip("@")
            if domain and "." in domain and " " not in domain and domain not in out:
                out.append(domain)
    return out


def _extract_domains(payload) -> list[str]:
    if not isinstance(payload, dict):
        return _normalize_domains(payload)
    data = payload.get("data") if "data" in payload else payload
    candidates = []
    if isinstance(data, dict):
        for key in ("domainList", "domains", "domain", "emailDomains", "mailDomains", "availDomains"):
            candidates.append(data.get(key))
    candidates.append(data)
    for item in candidates:
        domains = _normalize_domains(item)
        if domains:
            return domains
    return []


def _login_token() -> str:
    email = str(getattr(_email_cfg, "CLOUDMAIL_ADMIN_EMAIL", "") or "").strip()
    password = str(getattr(_email_cfg, "CLOUDMAIL_PASSWORD", "") or "").strip()
    if not email or not password:
        raise CloudMailError("CloudMail 管理员邮箱或密码为空，无法登录获取域名列表")
    payload = _request_raw("POST", "/api/login", body={"email": email, "password": password})
    token = _extract_token(payload)
    logger.info("[CloudMail] 管理员登录成功，可用于获取隐藏域名列表")
    return token


def fetch_domains(force: bool = False) -> list[str]:
    """从 CloudMail 平台获取可用邮箱域名列表。

    优先读取公开网站配置 `/api/setting/websiteConfig`；如果站点开启了登录页隐藏域名，
    再用管理员账号登录并读取 `/api/setting/query`。
    """
    global _DOMAIN_CACHE
    now = time.monotonic()
    if not force and _DOMAIN_CACHE and now - _DOMAIN_CACHE[0] < DOMAIN_CACHE_TTL:
        return list(_DOMAIN_CACHE[1])

    errors: list[str] = []
    for method, path, token in (
        ("GET", "/api/setting/websiteConfig", None),
        ("GET", "/api/setting/query", "__login__"),
    ):
        try:
            auth = _login_token() if token == "__login__" else token
            payload = _request_raw(method, path, token=auth)
            domains = _extract_domains(payload)
            if domains:
                _DOMAIN_CACHE = (now, domains)
                logger.info("[CloudMail] 已从平台获取可用域名：%s", ",".join(domains))
                return list(domains)
            errors.append(f"{path}: 响应中没有 domainList")
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")

    raise CloudMailError("CloudMail 自动获取域名失败：" + "；".join(errors))


def _random_local_part(length: int | None = None) -> str:
    length = int(length or getattr(_email_cfg, "CLOUDMAIL_RANDOM_LOCAL_LENGTH", 12) or 12)
    length = max(6, min(32, length))
    alphabet = string.ascii_lowercase + string.digits
    # 首字符用字母，减少邮箱服务商对纯数字/特殊前缀的兼容问题。
    return random.choice(string.ascii_lowercase) + "".join(secrets.choice(alphabet) for _ in range(length - 1))


def generate_email() -> str:
    domain = random.choice(_domains())
    return f"{_random_local_part()}@{domain}"


def _add_user(email: str) -> None:
    password = secrets.token_urlsafe(12)
    _request("/api/public/addUser", {"list": [{"email": email, "password": password}]})


def pick_account() -> CloudMailAccount:
    email = generate_email()
    domain = email.rsplit("@", 1)[1]
    if bool(getattr(_email_cfg, "CLOUDMAIL_AUTO_ADD_USER", True)):
        _add_user(email)
        logger.info("[CloudMail] 已添加随机邮箱用户: %s", email)
    else:
        logger.info("[CloudMail] 已生成随机邮箱: %s（未调用 addUser）", email)
    account = CloudMailAccount(email=email, domain=domain)
    _CONTEXT_CACHE[_cache_key(email)] = account
    return account


def get_email() -> str:
    return pick_account().email


def get_account_context(email: str) -> CloudMailAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info("[CloudMail] 已释放邮箱上下文: %s（status=%s, note=%s）", email, status, note or "")


def _parse_time(raw) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            # 文档标注 createTime 为 UTC。
            return datetime.strptime(text.replace("Z", ""), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _otp_item(mail: dict) -> dict:
    return {
        "id": mail.get("emailId") or mail.get("id"),
        "from": mail.get("sendEmail") or mail.get("from") or "",
        "subject": mail.get("subject") or "",
        "text": mail.get("text") or "",
        "html": mail.get("content") or mail.get("html") or "",
    }


def fetch_otp_once(
    email: str,
    after_ts: float | None,
    state: CloudMailProbeState,
    settle_seconds: int | None = None,
) -> OTPProbeResult:
    """单次 CloudMail 取码 probe；不 sleep、不维护总 deadline。"""
    target = str(email or "").strip()
    if not target:
        raise CloudMailError("CloudMail 取码缺少邮箱地址")

    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    mails = _request("/api/public/emailList", {"toEmail": target, "timeSort": "desc", "type": 0, "isDel": 0, "num": 1, "size": 20})
    if not isinstance(mails, list):
        raise CloudMailError("CloudMail 邮件查询响应 data 不是数组")
    for mail in sorted(mails, key=lambda item: _parse_time((item or {}).get("createTime")) or float("-inf"), reverse=True):
        if not isinstance(mail, dict):
            continue
        ts = _parse_time(mail.get("createTime"))
        if after_ts is not None and ts is not None and ts < after_ts - 30:
            continue
        item = _otp_item(mail)
        if not looks_like_openai_email(item):
            continue
        otp = extract_otp(item)
        if not otp:
            continue
        candidate_time = float("-inf") if ts is None else ts
        if state.best_otp is None or candidate_time > state.best_timestamp or (candidate_time == state.best_timestamp and otp != state.best_otp):
            state.best_otp = otp
            state.best_timestamp = candidate_time
            state.settle_until = time.monotonic() + settle
            logger.info("[CloudMail] 锁定 OTP 候选，等待 %ss 确认", settle)
    now = time.monotonic()
    if state.best_otp and state.settle_until is not None and now >= state.settle_until:
        return OTPProbeResult.completed(state.best_otp, state)
    return OTPProbeResult.candidate(state, state.settle_until) if state.best_otp else OTPProbeResult.pending(state)


def fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None) -> str:
    target = str(email or "").strip()
    if not target:
        raise CloudMailError("CloudMail 取码缺少邮箱地址")
    state = CloudMailProbeState()
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), _email_cfg.OTP_MAX_WAIT)
    return wait_for_otp_with_policy(provider="cloudmail", email=target, initial_after_ts=after_ts if after_ts is not None else time.time(), fetch=lambda stamp: fetch_otp_once(target, stamp, state, settle_seconds), retrigger=lambda: time.time(), wait_timeout=timeout, poll_interval=poll_interval or _email_cfg.OTP_POLL_INTERVAL, retry_count=0)
