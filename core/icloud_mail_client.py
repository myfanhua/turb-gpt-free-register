# -*- coding: utf-8 -*-
"""iCloud Pickup API client with per-mailbox credential isolation."""
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from config import email as _email_cfg
from core import db
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)


class ICloudMailError(RuntimeError):
    pass


@dataclass(frozen=True)
class ICloudMailAccount:
    email: str
    token: str
    pickup_url: str = ""


_CONTEXT_CACHE: dict[str, ICloudMailAccount] = {}
_PROFILE_SYNC_LOCK = threading.Lock()
_PROFILE_SYNC_CACHE_TOKEN = ""
_PROFILE_SYNC_CACHE_AT = 0.0
_PROFILE_SYNC_CACHE_PAYLOAD: object | None = None
_PROFILE_SYNC_CACHE_TTL = 1.0


@dataclass(frozen=True)
class _ProfileSyncResponse:
    status_code: int
    headers: dict
    payload: object

    def json(self):
        return self.payload


def _reset_profile_sync_cache() -> None:
    global _PROFILE_SYNC_CACHE_TOKEN, _PROFILE_SYNC_CACHE_AT, _PROFILE_SYNC_CACHE_PAYLOAD
    with _PROFILE_SYNC_LOCK:
        _PROFILE_SYNC_CACHE_TOKEN = ""
        _PROFILE_SYNC_CACHE_AT = 0.0
        _PROFILE_SYNC_CACHE_PAYLOAD = None


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def pick_account() -> ICloudMailAccount:
    row = db.claim_next_icloud_email()
    if row is None and _profile_token():
        disabled = db.list_icloud_email_pool(status="disabled", limit=5000)
        for item in disabled:
            note = str(item.get("note") or "")
            if note.startswith(("iCloud Pickup HTTP 401", "iCloud Pickup HTTP 403")):
                db.release_icloud_email(
                    item.get("email") or "",
                    status="available",
                    note="已切换到 iCloud Profile 同步",
                )
        row = db.claim_next_icloud_email()
    if row is None:
        raise ICloudMailError(f"iCloud 邮箱池没有可用邮箱: {db.icloud_email_pool_summary()}")
    account = ICloudMailAccount(
        email=row["email"],
        token=row["token"],
        pickup_url=str(row.get("pickup_url") or row.get("pickupUrl") or "").strip(),
    )
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info("[iCloud] 已领取邮箱: %s（DB id=%s）", account.email, row.get("id"))
    return account


def get_account_context(email: str) -> ICloudMailAccount | None:
    key = _cache_key(email)
    cached = _CONTEXT_CACHE.get(key)
    if cached is not None:
        return cached
    row = db.get_icloud_email_by_email(key, include_token=True)
    if row is None:
        return None
    account = ICloudMailAccount(
        email=row["email"],
        token=row["token"],
        pickup_url=str(row.get("pickup_url") or row.get("pickupUrl") or "").strip(),
    )
    _CONTEXT_CACHE[key] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    current = db.get_icloud_email_by_email(email)
    if current and current.get("status") == "disabled" and status == "available":
        status = "disabled"
    db.release_icloud_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(_cache_key(email), None)


def _api_url(pickup_url: str = "") -> str:
    custom = str(pickup_url or "").strip()
    if custom.startswith(("http://", "https://")):
        parsed = urlparse(custom)
        path = parsed.path.rstrip("/").lower()
        # 浏览器取件页把凭据放在 URL Fragment 中并返回 HTML，不是 JSON API。
        # 只接受明确的 API endpoint/base，其他链接回退到全局 Pickup API。
        if not parsed.fragment and path.endswith("/messages/latest"):
            return custom.rstrip("/")
        if not parsed.fragment and "/api/" in path and path.endswith("/pickup"):
            return f"{custom.rstrip('/')}/messages/latest"
    base = str(getattr(_email_cfg, "ICLOUD_PICKUP_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ICloudMailError("请填写 iCloud Pickup API 地址")
    return f"{base}/messages/latest"


def _message_timestamp(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 1e12 else value
    text = str(raw).strip()
    try:
        value = float(text)
        return value / 1000.0 if value > 1e12 else value
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _recipient_matches(value, target: str) -> bool:
    target = _cache_key(target)
    if isinstance(value, str):
        return target in value.lower()
    if isinstance(value, list):
        return any(_recipient_matches(item, target) for item in value)
    if isinstance(value, dict):
        return any(_recipient_matches(item, target) for item in value.values())
    return False


def _response_message(payload: object, target: str, after_ts: float | None) -> tuple[dict, float, str]:
    if not isinstance(payload, dict):
        raise ICloudMailError("iCloud Pickup 响应不是 JSON 对象")
    response_email = _cache_key(payload.get("email"))
    if response_email != _cache_key(target):
        raise ICloudMailError(
            f"iCloud Pickup 响应邮箱不匹配: expected={target}, actual={response_email or '-'}"
        )
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ICloudMailError("iCloud Pickup 响应缺少 message 对象")
    if not _recipient_matches(message.get("to"), target):
        raise ICloudMailError(f"iCloud Pickup 收件人不匹配: expected={target}")
    stamp = _message_timestamp(message.get("date"))
    if stamp is None:
        raise ICloudMailError(f"iCloud Pickup 邮件时间无效: email={target}")
    if after_ts is not None and stamp < float(after_ts) - 30:
        return message, stamp, "old"
    return message, stamp, "new"


def _request_latest(account: ICloudMailAccount):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {account.token}",
        "X-Mailbox-Email": account.email,
        "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
    }
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_PICKUP_TIMEOUT", 15) or 15))
    return requests.get(_api_url(account.pickup_url), headers=headers, timeout=timeout)


def _profile_token() -> str:
    return str(getattr(_email_cfg, "ICLOUD_PROFILE_TOKEN", "") or "").strip()


def _profile_api_url() -> str:
    base = str(getattr(_email_cfg, "ICLOUD_PROFILE_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ICloudMailError("请填写 iCloud Profile API 地址")
    return f"{base}/mail/sync"


def _request_profile_sync():
    global _PROFILE_SYNC_CACHE_TOKEN, _PROFILE_SYNC_CACHE_AT, _PROFILE_SYNC_CACHE_PAYLOAD
    token = _profile_token()
    headers = {
        "Accept": "application/json",
        "X-Profile-Token": token,
        "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
    }
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_PICKUP_TIMEOUT", 15) or 15))
    with _PROFILE_SYNC_LOCK:
        now = time.monotonic()
        if (
            _PROFILE_SYNC_CACHE_PAYLOAD is not None
            and _PROFILE_SYNC_CACHE_TOKEN == token
            and now - _PROFILE_SYNC_CACHE_AT <= _PROFILE_SYNC_CACHE_TTL
        ):
            return _ProfileSyncResponse(200, {}, _PROFILE_SYNC_CACHE_PAYLOAD)

        changes: list = []
        cursor = ""
        for _page in range(20):
            body = {"cursor": cursor} if cursor else {}
            response = requests.post(
                _profile_api_url(),
                headers=headers,
                json=body,
                timeout=timeout,
            )
            if int(response.status_code) != 200:
                return response
            payload = response.json()
            if not isinstance(payload, dict):
                return _ProfileSyncResponse(200, dict(response.headers), payload)
            page_changes = payload.get("changes")
            if isinstance(page_changes, list):
                changes.extend(page_changes)
            if not payload.get("hasMore"):
                merged = dict(payload)
                merged["changes"] = changes
                merged["hasMore"] = False
                _PROFILE_SYNC_CACHE_TOKEN = token
                _PROFILE_SYNC_CACHE_AT = time.monotonic()
                _PROFILE_SYNC_CACHE_PAYLOAD = merged
                return _ProfileSyncResponse(200, dict(response.headers), merged)
            next_cursor = str(payload.get("cursor") or "").strip()
            if not next_cursor or next_cursor == cursor:
                raise ICloudMailError("iCloud Profile 分页游标无效")
            cursor = next_cursor
        raise ICloudMailError("iCloud Profile 同步分页超过上限")


def _profile_response_message(
    payload: object,
    target: str,
    after_ts: float | None,
) -> tuple[dict, float, str] | None:
    if not isinstance(payload, dict):
        raise ICloudMailError("iCloud Profile 响应不是 JSON 对象")
    changes = payload.get("changes")
    if not isinstance(changes, list):
        raise ICloudMailError("iCloud Profile 响应缺少 changes 数组")

    candidates: list[dict] = []
    for change in changes:
        if not isinstance(change, dict) or change.get("operation") != "upsert":
            continue
        if _cache_key(change.get("account")) != _cache_key(target):
            continue
        summary = change.get("summary")
        if not isinstance(summary, dict):
            continue
        message = {
            "uid": summary.get("uid") or change.get("uid"),
            "mailbox": summary.get("mailbox") or change.get("mailbox"),
            "to": summary.get("to"),
            "date": summary.get("date"),
            "from": summary.get("from"),
            "subject": summary.get("subject"),
            "text": summary.get("text") or summary.get("preview") or "",
            "html": summary.get("html") or "",
        }
        stamp = _message_timestamp(message.get("date"))
        if stamp is not None:
            candidates.append({"message": message, "stamp": stamp})

    if not candidates:
        return None
    fallback = None
    for candidate in sorted(candidates, key=lambda item: item["stamp"], reverse=True):
        try:
            result = _response_message(
                {"email": target, "message": candidate["message"]},
                target,
                after_ts,
            )
        except ICloudMailError:
            continue
        if fallback is None:
            fallback = result
        message, _stamp, freshness = result
        if freshness == "new" and looks_like_openai_email(message) and extract_otp(message):
            return result
    return fallback


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    account = get_account_context(email)
    if account is None:
        raise ICloudMailError(f"iCloud 邮箱不存在或未领取: {email}")
    wait_seconds = max(0, int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT))
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + wait_seconds
    best_otp: str | None = None
    best_key = ""
    settle_until: float | None = None
    last_error = "尚未出现新的 OpenAI 验证码"
    first_attempt = True

    # max_wait=0 still performs one request, which is useful for explicit
    # single-shot callers and deterministic status/identity error reporting.
    while first_attempt or time.monotonic() <= deadline:
        first_attempt = False
        has_profile = bool(_profile_token())
        sources = [
            (False, "iCloud Pickup", lambda: _request_latest(account)),
        ]
        if has_profile:
            sources.append((True, "iCloud Profile", _request_profile_sync))

        for using_profile, source_name, request_source in sources:
            try:
                response = request_source()
                status = int(response.status_code)
                if status in {401, 403}:
                    if not using_profile and has_profile:
                        last_error = f"{source_name} HTTP {status}: 尝试 Profile 同步"
                        continue
                    if not using_profile:
                        db.release_icloud_email(
                            account.email,
                            status="disabled",
                            note=f"iCloud Pickup HTTP {status}",
                        )
                    raise ICloudMailError(f"{source_name} HTTP {status}: 凭据无效、到期或停用")
                if status == 404:
                    last_error = "当前邮箱没有可读取的邮件"
                    if not using_profile and has_profile:
                        continue
                elif status == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        interval = max(interval, min(30, int(float(retry_after))))
                    except (TypeError, ValueError):
                        interval = max(interval, 3)
                    last_error = "请求过于频繁"
                    if not using_profile and has_profile:
                        continue
                elif status == 503:
                    last_error = "邮箱正在初始化或暂时无法刷新"
                    if not using_profile and has_profile:
                        continue
                elif status >= 500:
                    last_error = f"服务暂时异常: HTTP {status}"
                    if not using_profile and has_profile:
                        continue
                elif status != 200:
                    if not using_profile and has_profile:
                        last_error = f"{source_name} HTTP {status}: 尝试 Profile 同步"
                        continue
                    raise ICloudMailError(f"{source_name} HTTP {status}: email={account.email}")
                else:
                    result = (
                        _profile_response_message(response.json(), account.email, after_ts)
                        if using_profile
                        else _response_message(response.json(), account.email, after_ts)
                    )
                    if result is None:
                        last_error = "浏览器资料中尚未出现该邮箱的新邮件"
                        if not using_profile and has_profile:
                            continue
                    else:
                        message, stamp, freshness = result
                        if freshness == "old":
                            last_error = "最新邮件早于本次验证码请求"
                            if not using_profile and has_profile:
                                continue
                        elif not looks_like_openai_email(message):
                            last_error = "最新邮件不是 OpenAI 验证邮件"
                            if not using_profile and has_profile:
                                continue
                        else:
                            code = extract_otp(message)
                            if code:
                                key = f"{message.get('uid') or ''}:{stamp}:{code}"
                                if key != best_key:
                                    best_key = key
                                    best_otp = code
                                    settle_until = time.monotonic() + settle
                                break
            except ICloudMailError:
                if not using_profile and has_profile:
                    continue
                raise
            except Exception as exc:
                # Keep diagnostic context useful while ensuring mailbox and
                # profile credentials are never included in errors or logs.
                detail = str(exc)
                if account.token:
                    detail = detail.replace(account.token, "***")
                profile_token = _profile_token()
                if profile_token:
                    detail = detail.replace(profile_token, "***")
                last_error = f"{type(exc).__name__}: {detail}"
                if not using_profile and has_profile:
                    continue

        now = time.monotonic()
        if best_otp and settle_until is not None and now >= settle_until:
            return best_otp
        if now >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - now)))

    if best_otp:
        return best_otp
    raise ICloudMailError(f"等待 iCloud 验证码超时: {account.email}; {last_error}")
