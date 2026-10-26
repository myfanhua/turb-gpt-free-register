# -*- coding: utf-8 -*-
"""西瓜（xigua.dasiqo.com）邮箱接码 API 客户端。"""
from __future__ import annotations
import logging, re, threading, time, uuid
from dataclasses import dataclass
import requests
from config import email as _cfg
from core.task_control import check_stop_requested as _check_task_stop, sleep as _task_sleep
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)
_CODE_RE = re.compile(r"\b(\d{6})\b")
_CACHE: dict[str, "XiguaAccount"] = {}
_LAST_CODES: dict[str, str] = {}
_LOCK = threading.RLock()

class XiguaError(RuntimeError): pass

@dataclass
class XiguaAccount:
    email: str
    order_id: str

def _base():
    return str(getattr(_cfg, "XIGUA_API_BASE", "https://xigua.dasiqo.com") or "https://xigua.dasiqo.com").rstrip("/")

def _key():
    key = str(getattr(_cfg, "XIGUA_API_KEY", "") or "").strip()
    if not key: raise XiguaError("西瓜 API Key 未配置，请在配置 → 邮箱 / OTP 填写 XIGUA_API_KEY")
    return key

def _request(method, path, *, body=None, auth=True, params=None, extra_headers=None):
    headers = {"Accept": "application/json"}
    if auth: headers["Authorization"] = "Bearer " + _key()
    if body is not None: headers["Content-Type"] = "application/json"
    if extra_headers: headers.update(extra_headers)
    try:
        r = requests.request(method, _base() + path, json=body, params=params, headers=headers,
                             timeout=max(1, min(120, int(getattr(_cfg, "XIGUA_REQUEST_TIMEOUT", 20) or 20))))
    except requests.RequestException as exc:
        raise XiguaError(f"西瓜请求失败 ({method} {path}): {exc}") from exc
    try: payload = r.json()
    except ValueError as exc: raise XiguaError(f"西瓜响应不是 JSON ({path})") from exc
    if r.status_code >= 400:
        err = payload.get("error") if isinstance(payload, dict) else None
        msg = err.get("message") if isinstance(err, dict) else payload.get("message") if isinstance(payload, dict) else ""
        raise XiguaError(f"西瓜请求失败 ({path}): HTTP {r.status_code}; {msg or payload}")
    return payload

def _unwrap(payload):
    if isinstance(payload, dict):
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return payload

def pick_account() -> XiguaAccount:
    wanted = str(getattr(_cfg, "XIGUA_OFFER_ID", "") or "").strip()
    if not wanted:
        raise XiguaError("西瓜 Offer ID 未配置，请在配置 → 邮箱 / OTP → 西瓜填写 XIGUA_OFFER_ID")
    # 固定使用配置中的 offer_id，不自动查询或切换其他方案。
    offer = {"id": wanted}
    payload = _request("POST", "/api/v1/orders", body={"offer_id": offer["id"]},
                       params=None, auth=True, extra_headers={"Idempotency-Key": "turb-xigua-" + str(uuid.uuid4())})
    data = _unwrap(payload)
    email, order_id = str(data.get("email") or "").strip(), str(data.get("id") or "").strip()
    if not email or not order_id: raise XiguaError(f"西瓜下单响应缺少 email/id: {data}")
    account = XiguaAccount(email, order_id)
    with _LOCK: _CACHE[email.lower()] = account
    logger.info("[Xigua] 已领取邮箱: %s order=%s", email, order_id)
    return account

def get_email(): return pick_account().email
def get_account_context(email):
    with _LOCK: return _CACHE.get(str(email or "").strip().lower())

def get_account_context_metadata(email):
    account = get_account_context(email)
    if account is None:
        return None
    return {"source": "xigua", "email": account.email, "order_id": account.order_id, "api_base": _base()}

def restore_account_context(email):
    """先恢复注册时的准确订单，旧账号才回退只读订单列表；不重新下单。"""
    from core.email_provider import get_saved_email_context_metadata
    target = str(email or "").strip()
    cached = get_account_context(target)
    if cached is not None:
        return cached
    metadata = get_saved_email_context_metadata(target, "xigua")
    order_id = metadata.get("order_id")
    if metadata and metadata.get("api_base") != _base():
        raise XiguaError("西瓜 API 地址已变更，停止使用原订单上下文")
    if isinstance(order_id, str) and order_id.strip():
        account = XiguaAccount(target, order_id)
        with _LOCK: _CACHE[target.lower()] = account
        return account
    payload = _request("GET", "/api/v1/orders")
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    items = data if isinstance(data, list) else (data.get("items", []) if isinstance(data, dict) else [])
    for item in items:
        if (isinstance(item, dict) and str(item.get("email") or "").strip().casefold() == target.casefold()
                and item.get("id")):
            account = XiguaAccount(target, str(item["id"]))
            with _LOCK: _CACHE[target.lower()] = account
            return account
    return None
def release_account(email, status="available", note=None):
    key = str(email or "").strip().lower()
    with _LOCK: account = _CACHE.pop(key, None)
    # 注册流程在邮箱尚未消耗时失败，应主动取消西瓜订单并释放平台库存/冻结资源。
    if account and str(status or "").lower() in {"available", "failed", "cancelled", "error"}:
        try:
            _request("POST", "/api/v1/orders/" + account.order_id + "/cancel", body={})
            logger.info("[Xigua] 已取消未消耗订单: %s order=%s", account.email, account.order_id)
        except Exception as exc:
            logger.warning("[Xigua] 取消订单失败: order=%s error=%s", account.order_id, exc)
    _LAST_CODES.pop(key, None)

def fetch_latest_otp(email, after_ts=0, max_wait=None, poll_interval=None, settle_seconds=None):
    _check_task_stop()
    account = get_account_context(email) or restore_account_context(email)
    if account is None: raise XiguaError(f"西瓜邮箱上下文不存在: {email}")
    deadline = time.monotonic() + int(max_wait if max_wait is not None else getattr(_cfg, "OTP_MAX_WAIT", 90) or 90)
    interval = max(1, int(poll_interval if poll_interval is not None else getattr(_cfg, "XIGUA_POLL_INTERVAL", 5) or 5))
    attempts = max(1, int(getattr(_cfg, "XIGUA_POLL_ATTEMPTS", 10) or 10))
    candidate = None
    confirmations = 0
    for _attempt in range(attempts):
        _check_task_stop()
        if time.monotonic() > deadline:
            break
        data = _unwrap(_request("GET", "/api/v1/orders/" + account.order_id))
        code = (data.get("code") or data.get("verification_code") or data.get("verificationCode")) if isinstance(data, dict) else None
        direct_code = False
        if code:
            m = _CODE_RE.search(str(code))
            if m:
                direct_code = True
                found = m.group(1)
                if found == candidate:
                    confirmations += 1
                else:
                    candidate, confirmations = found, 1
                if confirmations >= 2 and found != _LAST_CODES.get(account.email.lower()):
                    logger.info("[Xigua] 验证码已连续确认 2 次: %s", found)
                    _LAST_CODES[account.email.lower()] = found
                    return found
        text = " ".join(str(data.get(k, "")) for k in ("subject", "body", "body_preview", "message")) if isinstance(data, dict) else ""
        code = extract_otp({"subject": text, "body": text, "text": text})
        if code and not direct_code:
            if code == candidate:
                confirmations += 1
            else:
                candidate, confirmations = code, 1
            if confirmations >= 2 and code != _LAST_CODES.get(account.email.lower()):
                logger.info("[Xigua] 验证码已连续确认 2 次: %s", code)
                _LAST_CODES[account.email.lower()] = code
                return code
        _task_sleep(min(interval, max(0, deadline - time.monotonic())))
    raise XiguaError(f"西瓜验证码等待超时: {email}")
