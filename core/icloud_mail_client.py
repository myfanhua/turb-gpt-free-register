# -*- coding: utf-8 -*-
"""iCloud Pickup API client with per-mailbox credential isolation."""
import base64
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_to_bytes, urljoin, urlparse, urlsplit

import requests

from config import email as _email_cfg
from core import db
from core.icloud_pickup_page import (
    ICloudPickupPageError,
    parse_pickup_page,
    with_message_limit,
)
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)


class ICloudMailError(RuntimeError):
    pass


class ICloudProviderUnavailableError(ICloudMailError):
    pass


@dataclass(frozen=True)
class ICloudMailAccount:
    email: str
    token: str
    pickup_url: str = ""
    pickup_mode: str = "api_token"


_CONTEXT_CACHE: dict[str, ICloudMailAccount] = {}
_PROFILE_SYNC_LOCK = threading.Lock()
_PROFILE_SYNC_CACHE_TOKEN = ""
_PROFILE_SYNC_CACHE_AT = 0.0
_PROFILE_SYNC_CACHE_PAYLOAD: object | None = None
_PROFILE_SYNC_CACHE_TTL = 1.0
_PROVIDER_UNAVAILABLE_ROUNDS = 2
_HTML_PAGE_TIMEZONE = timezone(timedelta(hours=8))
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_QUERY_IN_TEXT_RE = re.compile(r"\?[^\s\"'<>]+")
_INDEPENDENT_DETAIL_BASE_RE = re.compile(
    r"\bdetailBase\s*=\s*(['\"])(?P<value>.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
_INDEPENDENT_DETAIL_SUFFIX_RE = re.compile(
    r"\bdetailSuffix\s*=\s*(['\"])(?P<value>.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
_INDEPENDENT_MESSAGE_ID_RE = re.compile(
    r"\bdata-id\s*=\s*['\"](?P<value>\d+)['\"]",
    re.IGNORECASE,
)


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


def _is_json_pickup_url(pickup_url: str) -> bool:
    parsed = urlparse(str(pickup_url or "").strip())
    path = parsed.path.rstrip("/").lower()
    return path.endswith("/messages/latest") or (
        "/api/" in path and path.endswith("/pickup")
    )


def _account_from_row(row: dict) -> ICloudMailAccount:
    token = str(row.get("token") or "").strip()
    pickup_url = str(row.get("pickup_url") or row.get("pickupUrl") or "").strip()
    pickup_mode = str(
        row.get("claimed_pickup_mode")
        or row.get("pickup_mode")
        or "api_token"
    ).strip()
    if pickup_mode == "independent_url":
        token = ""
    elif pickup_mode == "api_token" and pickup_url and not _is_json_pickup_url(pickup_url):
        pickup_url = ""
    return ICloudMailAccount(
        email=row["email"],
        token=token,
        pickup_url=pickup_url,
        pickup_mode=pickup_mode,
    )


def pick_account(selection: str = "all") -> ICloudMailAccount:
    selection = str(selection or "all").strip().lower()
    row = db.claim_next_icloud_email(pickup_filter=selection)
    if row is None and selection in {"all", "token"} and _profile_token():
        disabled = db.list_icloud_email_pool(status="disabled", limit=5000)
        for item in disabled:
            note = str(item.get("note") or "")
            if note.startswith(("iCloud Pickup HTTP 401", "iCloud Pickup HTTP 403")):
                db.release_icloud_email(
                    item.get("email") or "",
                    status="available",
                    note="已切换到 iCloud Profile 同步",
                )
        row = db.claim_next_icloud_email(pickup_filter=selection)
    if row is None:
        raise ICloudMailError(
            f"iCloud 邮箱池没有可用邮箱: "
            f"{db.icloud_email_pool_summary(pickup_filter=selection)}"
        )
    account = _account_from_row(row)
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
    account = _account_from_row(row)
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


def _message_timestamp(raw, assume_timezone=None) -> float | None:
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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None and assume_timezone is not None:
            parsed = parsed.replace(tzinfo=assume_timezone)
        return parsed.timestamp()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=assume_timezone or timezone.utc)
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


def _response_message(
    payload: object,
    target: str,
    after_ts: float | None,
    assume_timezone=None,
) -> tuple[dict, float, str]:
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
    stamp = _message_timestamp(message.get("date"), assume_timezone=assume_timezone)
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


def _request_independent_page(account: ICloudMailAccount):
    headers = {
        "Accept": "text/html",
        "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
    }
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_PICKUP_TIMEOUT", 15) or 15))
    return requests.get(
        with_message_limit(account.pickup_url, limit=10),
        headers=headers,
        timeout=timeout,
    )


def _redact_account_secrets(value: object, account: ICloudMailAccount) -> str:
    text = str(value or "")
    if account.token:
        text = text.replace(account.token, "***")
    if account.pickup_url:
        text = text.replace(account.pickup_url, "***")
        parsed = urlsplit(account.pickup_url)
        if parsed.path:
            text = text.replace(parsed.path, "/***")
        if parsed.query:
            text = text.replace(parsed.query, "***")
    profile_token = _profile_token()
    if profile_token:
        text = text.replace(profile_token, "***")
    text = _URL_IN_TEXT_RE.sub(
        lambda matched: _redacted_url(matched.group(0)),
        text,
    )
    return _QUERY_IN_TEXT_RE.sub("?***", text)


def _redacted_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "***"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme or 'https'}://{host}/***"


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


def _decode_independent_detail_body(value: object) -> str:
    text = str(value or "")
    if not text.lower().startswith("data:") or "," not in text:
        return text
    metadata, payload = text.split(",", 1)
    try:
        raw = (
            base64.b64decode(payload)
            if ";base64" in metadata.lower()
            else unquote_to_bytes(payload)
        )
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise ICloudMailError(
            f"iCloud 独立取件详情正文解码失败: {type(exc).__name__}"
        ) from exc


def _independent_detail_cards(response, account: ICloudMailAccount) -> list[dict] | None:
    html_text = str(getattr(response, "text", "") or "")
    base_match = _INDEPENDENT_DETAIL_BASE_RE.search(html_text)
    suffix_match = _INDEPENDENT_DETAIL_SUFFIX_RE.search(html_text)
    if not base_match and not suffix_match:
        return None
    if not base_match or not suffix_match:
        raise ICloudMailError("iCloud 独立取件页详情接口信息不完整")

    target = str(account.email or "").strip().lower()
    if target and target not in html_text.lower():
        raise ICloudMailError(f"iCloud 独立取件页面邮箱不匹配: expected={target}")

    message_ids = list(dict.fromkeys(
        matched.group("value")
        for matched in _INDEPENDENT_MESSAGE_ID_RE.finditer(html_text)
    ))[:10]
    if not message_ids:
        return []

    detail_base = base_match.group("value").replace(r"\/", "/")
    detail_suffix = suffix_match.group("value").replace(r"\/", "/")
    page_url = str(getattr(response, "url", "") or account.pickup_url)
    page_origin = urlsplit(page_url)
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_PICKUP_TIMEOUT", 15) or 15))
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
    }
    cards: list[dict] = []
    for message_id in message_ids:
        detail_url = urljoin(page_url, f"{detail_base}{message_id}{detail_suffix}")
        detail_origin = urlsplit(detail_url)
        if (
            detail_origin.scheme not in {"http", "https"}
            or detail_origin.scheme.lower() != page_origin.scheme.lower()
            or detail_origin.netloc.lower() != page_origin.netloc.lower()
        ):
            raise ICloudMailError("iCloud 独立取件详情接口跨域，已停止读取")
        detail_response = requests.get(detail_url, headers=headers, timeout=timeout)
        status = int(detail_response.status_code)
        if status != 200:
            raise ICloudMailError(f"iCloud 独立取件详情 HTTP {status}")
        try:
            payload = detail_response.json()
        except Exception as exc:
            raise ICloudMailError("iCloud 独立取件详情不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ICloudMailError("iCloud 独立取件详情格式无效")
        body = _decode_independent_detail_body(payload.get("body"))
        is_html = bool(payload.get("html")) or body.lstrip().lower().startswith(("<!doctype html", "<html"))
        cards.append({
            "uid": f"html-{message_id}",
            "to": account.email,
            "date": payload.get("receivedAt") or payload.get("date") or "",
            "from": payload.get("fromAddress") or payload.get("from") or "",
            "subject": payload.get("subject") or "",
            "body": "" if is_html else body,
            "html": body if is_html else "",
        })
    return cards


def _independent_response_message(
    response,
    account: ICloudMailAccount,
    after_ts: float | None,
) -> tuple[dict, float, str] | None:
    target = account.email
    cards = _independent_detail_cards(response, account)
    if cards is None:
        try:
            cards = parse_pickup_page(response.text, expected_email=target)
        except ICloudPickupPageError as exc:
            raise ICloudMailError(str(exc)) from exc
    candidates: list[tuple[dict, float, str]] = []
    for index, card in enumerate(cards):
        message = {
            "uid": card.get("uid") or f"html-{index}",
            "to": card.get("to") or target,
            "date": card.get("date") or "",
            "from": card.get("from") or "",
            "subject": card.get("subject") or "",
            "text": card.get("body") or "",
            "html": card.get("html") or "",
        }
        try:
            candidates.append(
                _response_message(
                    {"email": target, "message": message},
                    target,
                    after_ts,
                    assume_timezone=_HTML_PAGE_TIMEZONE,
                )
            )
        except ICloudMailError:
            continue
    if not candidates:
        return None
    fallback = None
    for result in sorted(candidates, key=lambda item: item[1], reverse=True):
        if fallback is None:
            fallback = result
        message, _stamp, freshness = result
        if freshness == "new" and looks_like_openai_email(message) and extract_otp(message):
            return result
    return fallback


def _account_sources(account: ICloudMailAccount) -> list[tuple[str, str, object]]:
    mode = str(account.pickup_mode or "api_token").strip().lower()
    sources: list[tuple[str, str, object]] = []
    if mode in {"independent_url", "independent_url_with_token"} and account.pickup_url:
        sources.append(("html", "iCloud 独立取件页", lambda: _request_independent_page(account)))
    if mode in {"api_token", "independent_url_with_token"} and account.token:
        sources.append(("pickup", "iCloud Pickup", lambda: _request_latest(account)))
    if account.token and _profile_token():
        sources.append(("profile", "iCloud Profile", _request_profile_sync))
    if not sources:
        raise ICloudMailError(f"iCloud 邮箱没有可用取件材料: {account.email}")
    return sources


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
    consecutive_unavailable_rounds = 0

    # max_wait=0 still performs one request, which is useful for explicit
    # single-shot callers and deterministic status/identity error reporting.
    while first_attempt or time.monotonic() <= deadline:
        first_attempt = False
        sources = _account_sources(account)
        unavailable_details: list[str] = []

        for source_index, (source_kind, source_name, request_source) in enumerate(sources):
            using_profile = source_kind == "profile"
            has_fallback = source_index < len(sources) - 1
            try:
                response = request_source()
                status = int(response.status_code)
                if status in {401, 403}:
                    if has_fallback:
                        last_error = f"{source_name} HTTP {status}: 尝试同邮箱后备来源"
                        continue
                    if not using_profile:
                        db.release_icloud_email(
                            account.email,
                            status="disabled",
                            note=f"{source_name} HTTP {status}",
                        )
                    raise ICloudMailError(f"{source_name} HTTP {status}: 凭据无效、到期或停用")
                if status == 404:
                    last_error = "当前邮箱没有可读取的邮件"
                    if has_fallback:
                        continue
                elif status == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        interval = max(interval, min(30, int(float(retry_after))))
                    except (TypeError, ValueError):
                        interval = max(interval, 3)
                    last_error = "请求过于频繁"
                    if has_fallback:
                        continue
                elif status == 503:
                    last_error = "邮箱正在初始化或暂时无法刷新"
                    unavailable_details.append(f"{source_name} HTTP {status}")
                    if has_fallback:
                        continue
                elif status >= 500:
                    last_error = f"服务暂时异常: HTTP {status}"
                    unavailable_details.append(f"{source_name} HTTP {status}")
                    if has_fallback:
                        continue
                elif status != 200:
                    if has_fallback:
                        last_error = f"{source_name} HTTP {status}: 尝试同邮箱后备来源"
                        continue
                    raise ICloudMailError(f"{source_name} HTTP {status}: email={account.email}")
                else:
                    if source_kind == "html":
                        result = _independent_response_message(response, account, after_ts)
                    elif using_profile:
                        result = _profile_response_message(response.json(), account.email, after_ts)
                    else:
                        result = _response_message(response.json(), account.email, after_ts)
                    if result is None:
                        last_error = f"{source_name}中尚未出现该邮箱的新邮件"
                        if has_fallback:
                            continue
                    else:
                        message, stamp, freshness = result
                        if freshness == "old":
                            last_error = "最新邮件早于本次验证码请求"
                            if has_fallback:
                                continue
                        elif not looks_like_openai_email(message):
                            last_error = "最新邮件不是 OpenAI 验证邮件"
                            if has_fallback:
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
                if has_fallback:
                    continue
                raise
            except requests.RequestException as exc:
                detail = _redact_account_secrets(exc, account)
                last_error = f"{type(exc).__name__}: {detail}"
                unavailable_details.append(f"{source_name} {type(exc).__name__}")
                if has_fallback:
                    continue
            except Exception as exc:
                # Keep diagnostic context useful while ensuring mailbox and
                # profile credentials are never included in errors or logs.
                detail = _redact_account_secrets(exc, account)
                last_error = f"{type(exc).__name__}: {detail}"
                if has_fallback:
                    continue

        if len(unavailable_details) == len(sources):
            consecutive_unavailable_rounds += 1
            if consecutive_unavailable_rounds >= _PROVIDER_UNAVAILABLE_ROUNDS:
                raise ICloudProviderUnavailableError(
                    "iCloud 接码服务连续异常: " + "；".join(unavailable_details)
                )
        else:
            consecutive_unavailable_rounds = 0

        now = time.monotonic()
        if best_otp and settle_until is not None and now >= settle_until:
            return best_otp
        if now >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - now)))

    if best_otp:
        return best_otp
    raise ICloudMailError(f"等待 iCloud 验证码超时: {account.email}; {last_error}")
