# -*- coding: utf-8 -*-
"""域名邮箱 MailCode：幂等购买、通道初始化及按邮箱游标取码。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from core.task_control import check_stop_requested as _check_task_stop, sleep as _task_sleep
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import requests

from config import email as _cfg

logger = logging.getLogger(__name__)
_PREFIX = "/api/open/v1"
_CONTEXT_CACHE: dict[str, "MailCodeAccount"] = {}
_LOCK = threading.RLock()
_PURCHASE_LOCK = threading.Lock()
_STATE_DIR = Path(__file__).resolve().parents[1] / "cache" / "mailcode"


class MailCodeError(RuntimeError):
    def __init__(self, message, *, status=0, code="", retry_after=0):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after


def _base():
    # 固定官方服务；不接受旧 .env 或运行时配置重定向 API Key。
    return "https://mailcode.bzb.qzz.io"


def _key():
    key = str(getattr(_cfg, "MAILCODE_API_KEY", "") or "").strip()
    if not key:
        raise MailCodeError("MailCode API Key 未配置（配置 → 邮箱 / OTP → 域名邮箱 MailCode）")
    return key


def _request(method, path, *, params=None, body=None, idempotency_key=None):
    headers = {"Authorization": "Bearer " + _key(), "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = requests.request(method, _base() + _PREFIX + path, params=params, json=body,
                                    headers=headers, timeout=max(1, int(_cfg.MAILCODE_REQUEST_TIMEOUT)),
                                    allow_redirects=False)
    except requests.RequestException as exc:
        # 不输出完整 URL/请求头，避免泄漏凭据。
        raise MailCodeError(f"MailCode 网络请求失败：{type(exc).__name__}") from exc
    try:
        delay = max(0, float(response.headers.get("Retry-After", 0)))
    except (TypeError, ValueError):
        delay = 0
    try:
        payload = response.json()
    except ValueError as exc:
        raise MailCodeError(f"MailCode 响应不是 JSON，HTTP {response.status_code}",
                            status=response.status_code, retry_after=delay) from exc
    if not isinstance(payload, dict):
        raise MailCodeError("MailCode 响应结构无效", status=response.status_code)
    error = payload.get("error") or {}
    if not 200 <= response.status_code < 300 or error:
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        raise MailCodeError(f"MailCode HTTP {response.status_code} {code}: {message}; request_id={payload.get('request_id', '-')}",
                            status=response.status_code, code=code, retry_after=delay)
    if "data" not in payload:
        raise MailCodeError("MailCode 响应缺少 data", status=response.status_code)
    return payload["data"]


def _pages(path):
    offset = 0
    while True:
        _check_task_stop()
        rows = _request("GET", path, params={"offset": offset, "limit": 100})
        if not isinstance(rows, list):
            raise MailCodeError("MailCode 分页响应 data 不是数组")
        yield from rows
        if len(rows) < 100:
            break
        offset += 100


def list_products():
    return list(_pages("/products"))


def get_wallet():
    return _request("GET", "/wallet")


def list_orders():
    return list(_pages("/orders"))


def get_order(order_id):
    return _request("GET", "/orders/" + quote(str(order_id), safe=""))


def list_mailboxes():
    return list(_pages("/mailboxes"))


def _mailbox_path(mailbox_id, suffix):
    return "/mailboxes/" + quote(str(mailbox_id), safe="") + suffix


def get_code_channel(mailbox_id):
    return _request("GET", _mailbox_path(mailbox_id, "/code-channel"))


def get_codes(mailbox_id, after_sequence=None):
    params = {"limit": 100}
    if after_sequence is not None:
        params["after_sequence"] = str(after_sequence)
    data = _request("GET", _mailbox_path(mailbox_id, "/codes"), params=params)
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise MailCodeError("MailCode 取码响应缺少 records 数组")
    return data


def export_code_links(ids):
    return _request("POST", "/mailboxes/code-links/export", body={"ids": ids})


def revoke_code_links(ids):
    return _request("POST", "/mailboxes/code-links/revoke", body={"ids": ids})


@dataclass
class MailCodeAccount:
    email: str
    mailbox_id: str
    order_id: str = ""
    next_sequence: str = "0"
    lock: object = field(default_factory=threading.RLock, repr=False)


def get_account_context(email):
    with _LOCK:
        return _CONTEXT_CACHE.get(str(email or "").strip().lower())


def _remember(account):
    with _LOCK:
        _CONTEXT_CACHE[account.email.lower()] = account
    return account


def _context_path(email):
    identity = hashlib.sha256((_base() + "\0" + _key() + "\0" + email.lower()).encode()).hexdigest()
    return _STATE_DIR / "contexts" / (identity + ".json")


def _save_context(account):
    _save_pending(_context_path(account.email), {
        "email": account.email, "mailbox_id": account.mailbox_id,
        "order_id": account.order_id, "next_sequence": account.next_sequence,
    })


def _cursor(data, current):
    value = str(data.get("next_sequence", current))
    if not re.fullmatch(r"[0-9]+", value) or int(value) > 9000000000000000000:
        raise MailCodeError("MailCode 返回了无效 next_sequence")
    return value  # 服务端游标原样使用，不能 +1，也不能按验证码数字去重。


def _prepare(account):
    """首次取码按需初始化 IMAP；READY 后才将地址交给注册流程。"""
    deadline = time.monotonic() + max(1, int(_cfg.MAILCODE_CHANNEL_WAIT_SECONDS))
    while True:
        _check_task_stop()
        try:
            data = get_codes(account.mailbox_id)  # 不传游标取得最新水位，不读取全部历史。
        except MailCodeError as exc:
            if exc.status not in {0, 429, 500, 502, 503, 504}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            _task_sleep(min(max(5, exc.retry_after), remaining))
            continue
        account.next_sequence = _cursor(data, account.next_sequence)
        if data.get("channel_status") == "READY":
            _save_context(account)
            return
        if data.get("channel_status") in {"PAUSED", "CLOSED"}:
            raise MailCodeError(f"MailCode 通道不可用：{data.get('channel_status')}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MailCodeError("MailCode 取码通道初始化超时；已购邮箱保留，下次领取继续初始化，不重新购买")
        _task_sleep(min(5, remaining))


def _pending_path(product):
    # 不保存 API Key；待确认购买持久化，断网/重启也继续使用原幂等键。
    identity = hashlib.sha256((_base() + "\0" + _key() + "\0" + product).encode()).hexdigest()
    return _STATE_DIR / "purchases" / (identity + ".json")


def _save_pending(path, pending):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(pending), encoding="utf-8")
    temp.replace(path)


def pick_account():
    product = str(getattr(_cfg, "MAILCODE_PRODUCT_ID", "product_a3783a0ead074b4091631b9ad04153f9") or "").strip()
    if not product:
        raise MailCodeError("MailCode 商品 ID 未配置，请填写 MAILCODE_PRODUCT_ID")
    with _PURCHASE_LOCK:
        path = _pending_path(product)
        pending = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"key": "turb-mailcode-" + uuid.uuid4().hex}
        _save_pending(path, pending)  # 必须在发起扣款前保存。
        if "item" not in pending:
            for attempt in range(3):
                _check_task_stop()
                try:
                    data = _request("POST", "/orders", body={"product_id": product, "count": 1}, idempotency_key=pending["key"])
                    if not isinstance(data, dict) or data.get("status") != "COMPLETED" or len(data.get("items") or []) != 1:
                        raise MailCodeError("MailCode 购买响应结构无效；保留原幂等键，勿重新下单")
                    item = data["items"][0]
                    if not item.get("mailbox_id") or "@" not in str(item.get("email", "")) or item.get("status") != "ACTIVE":
                        raise MailCodeError("MailCode 购买响应缺少有效邮箱，保留原幂等键")
                    pending.update(item=item, order_id=data.get("order_id", ""))
                    _save_pending(path, pending)
                    break
                except MailCodeError as exc:
                    retryable = exc.status == 0 or exc.status == 429 or exc.status >= 500
                    if not retryable:
                        if exc.status in {400, 401, 403, 404, 409, 413} and exc.code != "IDEMPOTENCY_CONFLICT":
                            path.unlink(missing_ok=True)
                        raise
                    if attempt == 2:
                        raise MailCodeError(f"{exc}；购买结果未确认，已保存幂等键，下次继续原请求") from exc
                    _task_sleep(max(1, exc.retry_after))
        item = pending["item"]
        account = _remember(MailCodeAccount(item["email"], item["mailbox_id"], pending.get("order_id", "")))
        _prepare(account)
        path.unlink(missing_ok=True)
    logger.info("[MailCode] 邮箱通道 READY：%s", account.email)
    return account


def get_email():
    return pick_account().email


def release_account(email, status="available", note=None):
    # 平台没有用户退款 API；释放本地上下文不能退款/撤销已购邮箱。
    with _LOCK:
        _CONTEXT_CACHE.pop(str(email or "").strip().lower(), None)


def _restore(email):
    path = _context_path(email)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        return _remember(MailCodeAccount(**saved))
    for row in _pages("/mailboxes"):
        if str(row.get("email", "")).lower() == email.lower() and row.get("id"):
            # 恢复时从历史游标 0 开始，并通过 after_ts/有效期过滤，不重新购买。
            return _remember(MailCodeAccount(row["email"], row["id"]))
    raise MailCodeError(f"MailCode 已购邮箱不存在或使用权失效：{email}")


def fetch_latest_otp(email, after_ts=None, max_wait=None, poll_interval=None, settle_seconds=None):
    _check_task_stop()
    account = get_account_context(email) or _restore(str(email).strip())
    deadline = time.monotonic() + max(0, int(_cfg.OTP_MAX_WAIT if max_wait is None else max_wait))
    interval = max(5, int(_cfg.OTP_POLL_INTERVAL if poll_interval is None else poll_interval))
    settle = max(0, int(_cfg.OTP_SETTLE_SECONDS if settle_seconds is None else settle_seconds))
    best, best_order, settle_until = None, (-1, -1), None
    last_error = "尚无新验证码"
    with account.lock:
        while True:
            _check_task_stop()
            delay = interval
            try:
                data = get_codes(account.mailbox_id, account.next_sequence)
                for record in data["records"]:
                    code = record.get("code")
                    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
                        continue
                    received = float(record.get("received_at") or 0) / 1000
                    expires = float(record.get("expires_at") or 0) / 1000
                    if (after_ts is not None and received < after_ts) or (expires and expires <= time.time()):
                        continue
                    order = (received, int(record.get("sequence") or 0))
                    if order > best_order:
                        best, best_order, settle_until = code, order, time.monotonic() + settle
                previous = account.next_sequence
                account.next_sequence = _cursor(data, previous)
                _save_context(account)
                if data.get("has_more"):
                    if account.next_sequence == previous:
                        raise MailCodeError("MailCode 分页游标未前进")
                    if time.monotonic() < deadline:
                        continue
                if best and time.monotonic() >= settle_until:
                    return best
                if data.get("channel_status") in {"CLOSED", "PAUSED"}:
                    raise MailCodeError("MailCode 取码通道已关闭/暂停", status=404)
            except MailCodeError as exc:
                if exc.status in {400, 401, 403, 404}:
                    raise
                last_error = str(exc)
                delay = max(interval, exc.retry_after)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _task_sleep(min(delay, remaining))
    if best:
        return best
    raise MailCodeError(f"等待 MailCode 验证码超时：{email}；{last_error}")
