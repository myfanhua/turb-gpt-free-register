# -*- coding: utf-8 -*-
"""iCloud Pickup API client with per-mailbox credential isolation."""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def pick_account() -> ICloudMailAccount:
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
        return custom.rstrip("/")
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
        try:
            response = _request_latest(account)
            status = int(response.status_code)
            if status in {401, 403}:
                db.release_icloud_email(
                    account.email,
                    status="disabled",
                    note=f"iCloud Pickup HTTP {status}",
                )
                raise ICloudMailError(f"iCloud Pickup HTTP {status}: 邮箱凭据无效、到期或停用")
            if status == 404:
                last_error = "当前邮箱没有可读取的邮件"
            elif status == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    interval = max(interval, min(30, int(float(retry_after))))
                except (TypeError, ValueError):
                    interval = max(interval, 3)
                last_error = "请求过于频繁"
            elif status == 503:
                last_error = "邮箱正在初始化或暂时无法刷新"
            elif status >= 500:
                last_error = f"服务暂时异常: HTTP {status}"
            elif status != 200:
                raise ICloudMailError(f"iCloud Pickup HTTP {status}: email={account.email}")
            else:
                message, stamp, freshness = _response_message(response.json(), account.email, after_ts)
                if freshness == "old":
                    last_error = "最新邮件早于本次验证码请求"
                elif not looks_like_openai_email(message):
                    last_error = "最新邮件不是 OpenAI 验证邮件"
                else:
                    code = extract_otp(message)
                    if code:
                        key = f"{message.get('uid') or ''}:{stamp}:{code}"
                        if key != best_key:
                            best_key = key
                            best_otp = code
                            settle_until = time.monotonic() + settle
        except ICloudMailError:
            raise
        except Exception as exc:
            # Keep diagnostic context useful while ensuring the mailbox Token
            # is never included in error strings or logs.
            detail = str(exc)
            if account.token:
                detail = detail.replace(account.token, "***")
            last_error = f"{type(exc).__name__}: {detail}"

        now = time.monotonic()
        if best_otp and settle_until is not None and now >= settle_until:
            return best_otp
        if now >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - now)))

    if best_otp:
        return best_otp
    raise ICloudMailError(f"等待 iCloud 验证码超时: {account.email}; {last_error}")
