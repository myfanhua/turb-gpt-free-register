# -*- coding: utf-8 -*-
"""Cloudflare Worker 临时邮箱客户端（cloudflare_temp_email 兼容）。

对齐 grokRegister-cpa/email_providers/cloudflare.py：
  - POST new_address / admin/new_address 自动创建邮箱，拿到 jwt
  - GET mails 轮询收件箱，提取 OpenAI 六位 OTP
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email
from core.otp_wait_policy import OTPProbeResult, resolve_wait_timeout, wait_for_otp_with_policy

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
_DOMAIN_COUNTER = 0
_DOMAIN_LOCK = threading.Lock()


class CFTempMailError(RuntimeError):
    """Cloudflare Worker 临时邮箱请求或取码失败。"""


@dataclass
class CFTempMailAccount:
    email: str
    jwt: str
    domain: str = ""
    created_at: float = 0.0


@dataclass
class CFTempMailProbeState:
    best_otp: str | None = None
    best_timestamp: float = float("-inf")
    best_message_key: str = ""
    settle_until: float | None = None


_CONTEXT_CACHE: dict[str, CFTempMailAccount] = {}


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _cfg_str(name: str, default: str = "") -> str:
    return str(getattr(_email_cfg, name, default) or default).strip()


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(getattr(_email_cfg, name, default) or default)
    except (TypeError, ValueError):
        return default


def _normalize_path(path: str, default: str) -> str:
    raw = str(path or default or "").strip() or default
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def _base_url() -> str:
    base = _cfg_str("CLOUDFLARE_API_BASE")
    if not base:
        raise CFTempMailError(
            "Cloudflare API 地址未配置，请填写 CLOUDFLARE_API_BASE（WebUI「配置 → 邮箱 / OTP」）。"
        )
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    return base.rstrip("/")


def _timeout() -> int:
    return max(5, _cfg_int("CLOUDFLARE_REQUEST_TIMEOUT", DEFAULT_TIMEOUT))


def _auth_mode() -> str:
    return _cfg_str("CLOUDFLARE_AUTH_MODE", "none").lower() or "none"


def _api_key() -> str:
    return _cfg_str("CLOUDFLARE_API_KEY")


def _custom_auth() -> str:
    return _cfg_str("CLOUDFLARE_CUSTOM_AUTH")


def _apply_custom_auth(headers: dict[str, str]) -> dict[str, str]:
    custom = _custom_auth()
    if custom:
        headers["x-custom-auth"] = custom
    return headers


def _build_headers(*, content_type: bool = False, bearer_jwt: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = "application/json"
    if bearer_jwt:
        headers["Authorization"] = f"Bearer {bearer_jwt}"
    else:
        key = _api_key()
        mode = _auth_mode()
        if key:
            if mode == "x-api-key":
                headers["X-API-Key"] = key
            elif mode == "x-admin-auth":
                headers["x-admin-auth"] = key
            elif mode == "bearer":
                headers["Authorization"] = f"Bearer {key}"
            elif mode not in ("none", "query-key"):
                headers["Authorization"] = f"Bearer {key}"
    return _apply_custom_auth(headers)


def _auth_params(params: dict | None = None) -> dict:
    merged = dict(params or {})
    key = _api_key()
    if key and _auth_mode() == "query-key":
        merged["key"] = key
    return merged


def _is_admin_create_path(path: str) -> bool:
    return str(path or "").rstrip("/").lower().endswith("/admin/new_address")


def _normalize_domains(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace(";", "\n").replace(",", "\n").splitlines()
    else:
        try:
            parts = list(raw)
        except TypeError:
            parts = [raw]
    out: list[str] = []
    for item in parts:
        domain = str(item or "").strip().lower().lstrip("@")
        if domain and "." in domain and " " not in domain and domain not in out:
            out.append(domain)
    return out


def _default_domains() -> list[str]:
    return _normalize_domains(getattr(_email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", []) or [])


def _next_domain() -> str:
    domains = _default_domains()
    if not domains:
        return ""
    global _DOMAIN_COUNTER
    with _DOMAIN_LOCK:
        domain = domains[_DOMAIN_COUNTER % len(domains)]
        _DOMAIN_COUNTER += 1
    return domain


def _generate_local(length: int | None = None) -> str:
    n = max(3, int(length if length is not None else _cfg_int("CLOUDFLARE_NAME_LENGTH", 10)))
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    bearer_jwt: str | None = None,
    content_type: bool = False,
) -> Any:
    url = _base_url() + _normalize_path(path, path or "/")
    headers = _build_headers(content_type=content_type or json_body is not None, bearer_jwt=bearer_jwt)
    query = _auth_params(params)
    try:
        response = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=json_body,
            params=query or None,
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise CFTempMailError(f"Cloudflare 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        text = (getattr(response, "text", "") or "")[:200]
        if response.status_code >= 400:
            raise CFTempMailError(
                f"Cloudflare 请求失败 ({path}): HTTP {response.status_code}; {text}"
            ) from exc
        raise CFTempMailError(
            f"Cloudflare 响应不是 JSON ({path}): HTTP {response.status_code}; {text}"
        ) from exc

    if response.status_code >= 400:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or payload.get("msg") or "")
        if not message:
            message = str(payload)[:200]
        hint = ""
        if response.status_code in (401, 403) and "turnstile" in message.lower():
            hint = "；匿名创建可能被 Turnstile 拦截，请改用 x-admin-auth + /admin/new_address"
        elif response.status_code in (401, 403) and _auth_mode() == "none":
            hint = "；可尝试 CLOUDFLARE_AUTH_MODE=x-admin-auth 并填写 ADMIN_PASSWORD"
        raise CFTempMailError(
            f"Cloudflare 请求失败 ({path}): HTTP {response.status_code}; {message}{hint}"
        )
    return payload


def _pick_list_payload(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "hydra:member", "data", "messages", "mails", "emails"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("messages", "mails", "emails", "results", "list"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def create_address(domain: str | None = None) -> CFTempMailAccount:
    """调用 Worker 创建临时邮箱，返回 address + jwt。"""
    accounts_path = _normalize_path(
        _cfg_str("CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address"),
        "/api/new_address",
    )
    selected_domain = str(domain or "").strip().lstrip("@") or _next_domain()
    mode = _auth_mode()
    key = _api_key()
    admin_create = _is_admin_create_path(accounts_path)

    if admin_create or mode in ("x-admin-auth", "bearer", "x-api-key", "query-key"):
        if not key:
            raise CFTempMailError(
                "Cloudflare admin/鉴权模式需要 CLOUDFLARE_API_KEY（ADMIN_PASSWORD）。"
            )

    if admin_create:
        payload: dict[str, Any] = {
            "name": _generate_local(),
            "enablePrefix": True,
        }
        if selected_domain:
            payload["domain"] = selected_domain
    else:
        # 匿名 /api/new_address：仅 domain；鉴权头仍按 AUTH_MODE 注入（若配置了 Key）
        payload = {}
        if selected_domain:
            payload["domain"] = selected_domain

    data = _request("POST", accounts_path, json_body=payload, content_type=True)
    if not isinstance(data, dict):
        raise CFTempMailError(f"Cloudflare 创建邮箱响应格式错误: {str(data)[:200]}")

    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    address = str(data.get("address") or nested.get("address") or nested.get("email") or data.get("email") or "").strip()
    jwt = str(data.get("jwt") or nested.get("jwt") or nested.get("token") or data.get("token") or "").strip()
    if not address or "@" not in address:
        raise CFTempMailError(f"Cloudflare 创建邮箱响应缺少 address: {str(data)[:200]}")
    if not jwt:
        raise CFTempMailError(f"Cloudflare 创建邮箱响应缺少 jwt: {str(data)[:200]}")

    domain_part = address.split("@", 1)[-1].lower()
    account = CFTempMailAccount(
        email=address,
        jwt=jwt,
        domain=domain_part,
        created_at=time.time(),
    )
    logger.info("[Cloudflare] 已创建临时邮箱: %s (domain=%s)", address, domain_part)
    return account


def pick_account() -> CFTempMailAccount:
    """创建并缓存一个 Cloudflare 临时邮箱。"""
    account = create_address()
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    return account


def get_account_context(email: str) -> CFTempMailAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info(
        "[Cloudflare] 已释放临时邮箱: %s（status=%s, note=%s）",
        email,
        status,
        note or "",
    )


def _message_timestamp(item: dict) -> float | None:
    """解析邮件时间。Worker 的 created_at 多为 UTC 且无时区后缀，必须按 UTC 解释。"""
    for key in ("timestamp", "created_at", "createdAt", "date", "receivedAt", "time"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 1e12:
                value = value / 1000.0
            return value
        text = str(raw).strip()
        if not text:
            continue
        try:
            if text.isdigit():
                value = float(text)
                if value > 1e12:
                    value /= 1000.0
                return value
            # 兼容 "2026-07-19 12:57:38" / ISO
            normalized = text.replace("Z", "+00:00")
            if "T" not in normalized and " " in normalized and "+" not in normalized[10:]:
                # 无时区的空格分隔时间：按 UTC 处理（cloudflare_temp_email 常见）
                dt = datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return None


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "")


def _message_text(item: dict) -> str:
    parts: list[str] = []
    for field in ("text", "raw", "content", "intro", "body", "snippet", "bodyText", "bodyPreview"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    html = item.get("html") or item.get("html_content") or item.get("bodyHtml")
    if isinstance(html, str) and html.strip():
        parts.append(_strip_html(html))
    elif isinstance(html, list):
        for chunk in html:
            if isinstance(chunk, str) and chunk.strip():
                parts.append(_strip_html(chunk))
    return "\n".join(parts)


def _message_addresses(item: dict) -> list[str]:
    out: list[str] = []
    for key in ("address", "to", "recipient", "recipients"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip().lower())
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and entry.strip():
                    out.append(entry.strip().lower())
                elif isinstance(entry, dict):
                    addr = entry.get("address") or entry.get("email") or entry.get("addr")
                    if addr:
                        out.append(str(addr).strip().lower())
    return out


def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out).strip()


def _parse_raw_email(raw: str) -> dict[str, str]:
    """把 cloudflare_temp_email 的 raw MIME 解析成 subject/from/text/html。"""
    result = {"subject": "", "from": "", "text": "", "html": ""}
    if not (raw or "").strip():
        return result
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8", errors="replace"))
    except Exception:
        # 回退：至少从头部抠 Subject/From
        for line in raw.splitlines():
            low = line.lower()
            if low.startswith("subject:") and not result["subject"]:
                result["subject"] = _decode_mime_header(line.split(":", 1)[1].strip())
            elif low.startswith("from:") and not result["from"]:
                result["from"] = line.split(":", 1)[1].strip()
        result["text"] = raw
        return result

    result["subject"] = _decode_mime_header(str(msg.get("subject") or ""))
    result["from"] = str(msg.get("from") or "").strip()
    texts: list[str] = []
    htmls: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
        if not isinstance(body, str):
            body = str(body)
        if ctype == "text/plain":
            texts.append(body)
        else:
            htmls.append(body)
    result["text"] = "\n".join(texts).strip()
    result["html"] = "\n".join(htmls).strip()
    if not result["text"] and not result["html"]:
        result["text"] = raw
    return result


def _standalone_otp_from_html(html: str) -> str | None:
    """OpenAI 邮件常见：验证码单独占一行/单元格，如 >449759<。"""
    if not html:
        return None
    # 去 style，减少 #353740 这类颜色误伤
    cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.I | re.S)
    for pattern in (
        r">\s*(\d{6})\s*<",
        r"(?m)^\s*(\d{6})\s*$",
    ):
        matches = re.findall(pattern, cleaned)
        # 过滤明显颜色/追踪号语境在上层做；这里取最后一个独立码更接近正文 OTP
        if matches:
            return matches[-1]
    return None


def _otp_item(item: dict) -> dict:
    raw = item.get("raw") if isinstance(item.get("raw"), str) else ""
    parsed = _parse_raw_email(raw) if raw else {"subject": "", "from": "", "text": "", "html": ""}

    sender = (
        item.get("from")
        or item.get("from_address")
        or item.get("sender")
        or item.get("source")  # cloudflare_temp_email 列表字段
        or parsed.get("from")
        or ""
    )
    subject = item.get("subject") or parsed.get("subject") or ""
    text = item.get("text") or parsed.get("text") or ""
    html = item.get("html") if isinstance(item.get("html"), str) else ""
    if not html:
        html = parsed.get("html") or ""

    # 若只有 raw 且 MIME 解析失败，保留非 header 的文本兜底
    if not text and not html and raw:
        text = raw

    # 独立 6 位码优先塞进 text 开头，帮助 extract_otp 命中正文 OTP 而非邮件头噪声
    standalone = _standalone_otp_from_html(html) or _standalone_otp_from_html(text)
    if standalone:
        text = f"verification code {standalone}\n{text}"

    return {
        "id": item.get("id") or item.get("msgid") or item.get("mail_id"),
        "from": sender,
        "subject": subject,
        "text": text,
        "html": html,
        "to": ", ".join(_message_addresses(item)),
    }


def _message_id(item: dict) -> str:
    return str(item.get("id") or item.get("msgid") or item.get("mail_id") or "").strip()


def list_messages(jwt: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
    """拉取收件箱。cloudflare_temp_email 的 /api/mails 要求有效 limit（否则 HTTP 400 Invalid limit）。"""
    path = _normalize_path(
        _cfg_str("CLOUDFLARE_PATH_MESSAGES", "/api/mails"),
        "/api/mails",
    )
    # 与 grokRegister-cpa 对齐：limit/offset 为必填查询参数
    safe_limit = max(1, min(100, int(limit or 20)))
    safe_offset = max(0, int(offset or 0))
    payload = _request(
        "GET",
        path,
        bearer_jwt=jwt,
        params={"limit": safe_limit, "offset": safe_offset},
    )
    return _pick_list_payload(payload)


def get_message_detail(jwt: str, message_id: str) -> dict:
    if not message_id:
        return {}
    messages_path = _normalize_path(
        _cfg_str("CLOUDFLARE_PATH_MESSAGES", "/api/mails"),
        "/api/mails",
    )
    candidates = [
        f"/api/mail/{message_id}",
        f"{messages_path}/{message_id}",
    ]
    last_error: Exception | None = None
    for path in candidates:
        try:
            payload = _request("GET", path, bearer_jwt=jwt)
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                return payload["data"]
            if isinstance(payload, dict):
                return payload
        except CFTempMailError as exc:
            last_error = exc
            continue
    if last_error:
        logger.debug("[Cloudflare] 邮件详情获取失败 id=%s: %s", message_id, last_error)
    return {}


def fetch_otp_once(
    email: str,
    after_ts: float | None,
    state: CFTempMailProbeState,
    settle_seconds: int | None = None,
) -> OTPProbeResult:
    """单次 Cloudflare 取码 probe；不 sleep、不维护总 deadline。"""
    target = str(email or "").strip()
    if not target:
        raise CFTempMailError("Cloudflare 取码缺少邮箱地址")

    account = get_account_context(target)
    if account is None or not account.jwt:
        raise CFTempMailError(
            f"Cloudflare 邮箱上下文缺失: {target}。请确认该地址由当前进程 cloudflare 来源创建。"
        )

    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    after = float(after_ts if after_ts is not None else time.time()) - 30
    target_lower = target.lower()

    messages = list_messages(account.jwt)
    if messages:
        logger.debug("[Cloudflare] 本轮收件 %s 封", len(messages))
    for item in messages:
            addresses = _message_addresses(item)
            if addresses and not any(target_lower == a or target_lower in a for a in addresses):
                continue

            detail = item
            msg_id = _message_id(item)
            otp_probe = _otp_item(detail)
            # 列表字段不足时尝试详情
            if (not otp_probe.get("text") and not otp_probe.get("html")) or not looks_like_openai_email(otp_probe):
                if msg_id:
                    fetched = get_message_detail(account.jwt, msg_id)
                    if fetched:
                        merged = dict(item)
                        merged.update(fetched)
                        detail = merged
                        otp_probe = _otp_item(detail)

            otp_item = otp_probe
            if not looks_like_openai_email(otp_item):
                logger.debug(
                    "[Cloudflare] 跳过非 OpenAI 邮件 id=%s from=%s subject=%s",
                    msg_id,
                    str(otp_item.get("from") or "")[:80],
                    str(otp_item.get("subject") or "")[:80],
                )
                continue

            ts = _message_timestamp(detail)
            if ts is not None and ts < after:
                logger.info(
                    "[Cloudflare] 跳过过旧邮件 id=%s ts=%s after=%s subject=%s",
                    msg_id,
                    int(ts),
                    int(after),
                    str(otp_item.get("subject") or "")[:60],
                )
                continue

            otp = extract_otp(otp_item)
            if not otp:
                logger.info(
                    "[Cloudflare] OpenAI 邮件未能抽取 6 位码 id=%s subject=%s",
                    msg_id,
                    str(otp_item.get("subject") or "")[:80],
                )
                continue

            message_key = msg_id or f"{otp_item.get('subject')}|{otp}|{ts}"
            effective_ts = ts if ts is not None else time.time()
            if effective_ts > state.best_timestamp or (
                effective_ts == state.best_timestamp and message_key != state.best_message_key and state.best_otp != otp
            ):
                if state.best_otp and state.best_otp != otp:
                    logger.info("[Cloudflare] 发现更晚 OTP=%s，替换 %s", otp, state.best_otp)
                elif not state.best_otp:
                    logger.info("[Cloudflare] 锁定 OTP 候选 %s，等待 settle=%ss", otp, settle)
                state.best_otp = otp
                state.best_timestamp = effective_ts
                state.best_message_key = message_key
                state.settle_until = time.monotonic() + settle
            elif message_key == state.best_message_key and state.best_otp == otp:
                # 同一封邮件不重置 settle
                pass

    now = time.monotonic()
    if state.best_otp and state.settle_until is not None and now >= state.settle_until:
        logger.info("[Cloudflare] settle 完成，返回 OTP=%s", state.best_otp)
        return OTPProbeResult.completed(state.best_otp, state)
    return OTPProbeResult.candidate(state, state.settle_until) if state.best_otp else OTPProbeResult.pending(state)


def fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None) -> str:
    """按统一策略轮询 Cloudflare 邮箱，返回领取时间后最新的 OpenAI 六位验证码。"""
    target = str(email or "").strip()
    if not target:
        raise CFTempMailError("Cloudflare 取码缺少邮箱地址")
    state = CFTempMailProbeState()
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), _email_cfg.OTP_MAX_WAIT)
    return wait_for_otp_with_policy(provider="cloudflare", email=target, initial_after_ts=after_ts if after_ts is not None else time.time(), fetch=lambda stamp: fetch_otp_once(target, stamp, state, settle_seconds), retrigger=lambda: time.time(), wait_timeout=timeout, poll_interval=poll_interval or _email_cfg.OTP_POLL_INTERVAL, retry_count=0)
