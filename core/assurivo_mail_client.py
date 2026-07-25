# -*- coding: utf-8 -*-
"""Assurivo 邮箱素材池与单次取码客户端。

素材和状态独立于 Outlook/generic_api：``email----查询码``。查询码只用于
Assurivo 的 ``pwd`` 参数，绝不写入日志或异常。
"""
from __future__ import annotations

import json
import html
import logging
import math
import re
import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from config import email as _email_cfg
from core.otp_wait_policy import OTPProbeResult, OTPWaitExhausted, resolve_wait_timeout, wait_for_otp_with_policy

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的Assurivo邮箱.txt"
_STATE_FILE = _PROJECT_ROOT / "用于注册的Assurivo邮箱.json"
_LOCK = threading.RLock()
_CONTEXT_CACHE: dict[str, "AssurivoAccount"] = {}
_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_VERIFY_WORDS = ("verification", "verify", "verification code", "code", "验证码", "认证", "認証")
_NON_VERIFY_WORDS = ("order", "invoice", "receipt", "purchase", "shipping", "订单", "发票", "收据")


class AssurivoMailError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssurivoAccount:
    email: str
    query_url: str

    @property
    def query_code(self) -> str:
        """兼容旧调用方；查询 URL 是唯一允许的凭据载体，禁止拆出或输出 pwd。"""
        return self.query_url


@dataclass
class AssurivoOtpState:
    code: str | None = None
    fingerprint: str | None = None
    ready_at_monotonic: float | None = None


def _redact_email(email: str) -> str:
    local, sep, domain = str(email or "").partition("@")
    return f"{local[:1]}***@{domain}" if sep else "***"


def parse_material_line(line: str) -> AssurivoAccount | None:
    parts = [part.strip() for part in str(line or "").strip().split("----", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1] or "@" not in parts[0]:
        return None
    try:
        parsed = urlparse(parts[1])
        # 素材必须为 Assurivo 完整查询 URL。请求时始终原样使用，不能重新编码
        # 或从中提取 pwd 再拼装，避免 URL 语义变化及敏感字段泄漏。
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("assurivo.com", "www.assurivo.com"):
            return None
        url_mail = (parse_qs(parsed.query, keep_blank_values=True).get("mail") or [""])[0].strip()
        if not url_mail or url_mail.casefold() != parts[0].casefold():
            return None
    except (TypeError, ValueError):
        return None
    return AssurivoAccount(email=parts[0], query_url=parts[1])


def _material_path() -> Path:
    configured = str(getattr(_email_cfg, "ASSURIVO_ACCOUNTS_FILE", "") or "").strip()
    if not configured or configured == _ACCOUNTS_FILE.name:
        return _ACCOUNTS_FILE
    path = Path(configured)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _state_path(material_path: Path) -> Path:
    return _STATE_FILE if material_path == _ACCOUNTS_FILE else material_path.with_suffix(".json")


def _load_state(path: Path) -> list[dict]:
    if not path.exists(): return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_state(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    material = Path(path) if path else _material_path()
    if not material.is_absolute(): material = _PROJECT_ROOT / material
    if not material.exists(): return 0, 0
    with _LOCK:
        state_path, rows = _state_path(material), _load_state(_state_path(material))
        existing = {(row.get("email") or "").lower() for row in rows}
        inserted = skipped = 0
        for raw in material.read_text(encoding="utf-8").splitlines():
            account = parse_material_line(raw)
            if account is None or account.email.lower() in existing:
                skipped += 1
                continue
            rows.append({"email": account.email, "query_url": account.query_url, "status": "available", "note": None, "used_at": None})
            existing.add(account.email.lower()); inserted += 1
        if inserted: _save_state(state_path, rows)
        return inserted, skipped


def import_records(records: list[dict]) -> tuple[int, int]:
    """导入已解析的 Assurivo 素材；查询 URL 原样保存，绝不记录其中的 pwd。"""
    material = _material_path()
    with _LOCK:
        state_path, rows = _state_path(material), _load_state(_state_path(material))
        by_email = {(row.get("email") or "").casefold(): row for row in rows}
        inserted = skipped = 0
        for raw in records:
            email = str(raw.get("email") or "").strip()
            query_url = str(raw.get("query_url") or raw.get("code_url") or "").strip()
            account = parse_material_line(f"{email}----{query_url}")
            if account is None or account.email.casefold() in by_email:
                skipped += 1
                continue
            status = str(raw.get("status") or "available")
            rows.append({
                "email": account.email,
                "query_url": account.query_url,
                "status": status,
                "note": raw.get("note"),
                "used_at": raw.get("used_at"),
                "imported_at": raw.get("imported_at") or datetime.now().isoformat(timespec="seconds"),
            })
            by_email[account.email.casefold()] = rows[-1]
            inserted += 1
        if inserted:
            _save_state(state_path, rows)
        return inserted, skipped


def migrate_legacy_generic_api_records() -> dict[str, int]:
    """把 generic_api 池中完整 Assurivo 查询 URL 转入独立状态池。

    先持久化目标状态、再移除 generic 行；若目标写入异常，源池保持不变。迁移保留
    ``used`` 等状态，避免已经领取的邮箱被重新领取。整个过程不输出查询 URL 或 pwd。
    """
    from core import db

    with db._LOCK, _LOCK:
        generic_rows = db._load_generic_api_emails()
        state_path, assurivo_rows = _state_path(_material_path()), _load_state(_state_path(_material_path()))
        by_email = {(row.get("email") or "").casefold(): row for row in assurivo_rows}
        retained: list[dict] = []
        moved = existing = 0
        changed_state = False
        for row in generic_rows:
            email = str(row.get("email") or "").strip()
            code_url = str(row.get("code_url") or "").strip()
            account = parse_material_line(f"{email}----{code_url}")
            if account is None:
                retained.append(row)
                continue
            prior = by_email.get(account.email.casefold())
            if prior is None:
                prior = {
                    "email": account.email,
                    "query_url": account.query_url,
                    "status": row.get("status") or "available",
                    "note": row.get("note"),
                    "used_at": row.get("used_at"),
                    "imported_at": row.get("imported_at") or datetime.now().isoformat(timespec="seconds"),
                }
                assurivo_rows.append(prior)
                by_email[account.email.casefold()] = prior
                changed_state = True
            else:
                # 目标已存在时，不覆盖其 URL；若旧行已领取/失败，采用更保守状态。
                if prior.get("status") == "available" and row.get("status") != "available":
                    prior["status"] = row.get("status")
                    prior["used_at"] = row.get("used_at") or prior.get("used_at")
                    prior["note"] = row.get("note") or prior.get("note")
                    changed_state = True
                existing += 1
            moved += 1
        if not moved:
            return {"migrated": 0, "existing": 0}
        if changed_state:
            _save_state(state_path, assurivo_rows)
        # 目标先成功落盘才会写回 generic；若此处异常，下次会再次安全检查，不会被 generic 领取。
        db._save_generic_api_emails(retained)
        return {"migrated": moved - existing, "existing": existing}


def list_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    """返回 WebUI 安全摘要；不回显 query_url/pwd。"""
    with _LOCK:
        rows = _load_state(_state_path(_material_path()))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda row: str(row.get("imported_at") or row.get("used_at") or ""), reverse=True)
        return [
            {key: row.get(key) for key in ("email", "status", "note", "used_at", "imported_at")}
            for row in rows[:limit]
        ]


def pool_summary() -> dict[str, int]:
    with _LOCK:
        out: dict[str, int] = {"available": 0, "used": 0, "failed": 0}
        for row in _load_state(_state_path(_material_path())):
            status = str(row.get("status") or "available")
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(value for key, value in out.items() if key != "total")
        return out


def delete_account(email: str) -> bool:
    material = _material_path()
    with _LOCK:
        state_path, rows = _state_path(material), _load_state(_state_path(material))
        target = str(email or "").casefold()
        retained = [row for row in rows if (row.get("email") or "").casefold() != target]
        if len(retained) == len(rows):
            return False
        _save_state(state_path, retained)
    _CONTEXT_CACHE.pop(email, None)
    return True


def pick_account() -> AssurivoAccount:
    material = _material_path(); import_from_file(material)
    with _LOCK:
        state_path, rows = _state_path(material), _load_state(_state_path(material))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            raise AssurivoMailError("Assurivo 邮箱池没有可用素材")
        row.update(status="used", used_at=datetime.now().isoformat(timespec="seconds"), note=None)
        _save_state(state_path, rows)
        account = AssurivoAccount(row["email"], row.get("query_url") or "")
        _CONTEXT_CACHE[account.email] = account
        logger.info("[Assurivo] 已领取邮箱: %s", _redact_email(account.email))
        return account


def get_account_context(email: str) -> AssurivoAccount | None:
    if email in _CONTEXT_CACHE: return _CONTEXT_CACHE[email]
    material = _material_path()
    with _LOCK:
        row = next((r for r in _load_state(_state_path(material)) if (r.get("email") or "").lower() == email.lower()), None)
    if not row: return None
    account = AssurivoAccount(row["email"], row.get("query_url") or "")
    _CONTEXT_CACHE[email] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    material = _material_path()
    with _LOCK:
        state_path, rows = _state_path(material), _load_state(_state_path(material))
        row = next((r for r in rows if (r.get("email") or "").lower() == email.lower()), None)
        if row:
            row["status"] = status; row["note"] = note
            row["used_at"] = None if status == "available" else row.get("used_at") or datetime.now().isoformat(timespec="seconds")
            _save_state(state_path, rows)
    _CONTEXT_CACHE.pop(email, None)


def release_unconsumed_account(email: str, note: str | None = None) -> bool:
    from core import db
    if db.get_account_by_email(email) is not None: return False
    material = _material_path()
    with _LOCK:
        state_path, rows = _state_path(material), _load_state(_state_path(material))
        row = next((r for r in rows if (r.get("email") or "").lower() == email.lower()), None)
        if not row or row.get("status") != "used": return False
        row.update(status="available", used_at=None, note=note)
        _save_state(state_path, rows)
    _CONTEXT_CACHE.pop(email, None)
    return True


def _parse_timestamp(message: dict) -> float | None:
    for key in ("timestamp", "time", "received_at", "created_at", "date"):
        value = message.get(key)
        if isinstance(value, (int, float)):
            return float(value / 1000 if value > 10_000_000_000 else value)
        if isinstance(value, str):
            try: return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
            except ValueError: pass
    return None


def _message_text(message: dict) -> tuple[str, str, str]:
    sender = " ".join(str(message.get(key) or "") for key in ("from", "from_address", "sender"))
    subject = str(message.get("subject") or "")
    body = "\n".join(str(message.get(key) or "") for key in ("text", "content", "body", "html"))
    return sender, subject, body


def looks_like_openai_verification(message: dict) -> bool:
    sender, subject, body = _message_text(message)
    combined = f"{sender}\n{subject}\n{body}".lower()
    identity = "openai" in combined or "chatgpt" in combined
    verification = any(word in combined for word in _VERIFY_WORDS)
    obvious_non_verification = any(word in combined for word in _NON_VERIFY_WORDS) and not verification
    return identity and verification and not obvious_non_verification


def extract_message_otp(message: dict) -> str | None:
    _, subject, body = _message_text(message)
    text = f"{subject}\n{body}"
    for match in _OTP_RE.finditer(text):
        window = text[max(0, match.start() - 80):match.end() + 80].lower()
        if any(word in window for word in _VERIFY_WORDS): return match.group(1)
    return None


def _message_dicts(value: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        if any(key in value for key in ("from", "from_address", "sender", "subject", "html", "text", "body")):
            found.append(value)
        for child in value.values(): found.extend(_message_dicts(child))
    elif isinstance(value, list):
        for child in value: found.extend(_message_dicts(child))
    return found


def _timestamp_is_fresh(stamp: float, after_ts: float, *, snapshot_available: bool) -> bool:
    """快照可用时允许有限时钟偏差；否则维持严格 after_ts。"""
    if not snapshot_available:
        return stamp > after_ts
    window = max(0, int(getattr(_email_cfg, "OTP_FRESHNESS_WINDOW_SECONDS", 180) or 0))
    return (after_ts - window) <= stamp <= (after_ts + window)


def extract_new_openai_otp(payload: Any, after_ts: float, known_otp_fingerprints: set[str] | None = None) -> str | None:
    """在时间新鲜度和快照指纹双门槛下提取 OpenAI OTP。"""
    snapshot_available = known_otp_fingerprints is not None
    known = known_otp_fingerprints or set()
    candidates = []
    for message in _message_dicts(payload):
        stamp = _parse_timestamp(message)
        # 无可靠来源时间的内容绝不用于自动注册，防止取到缓存旧码。
        if stamp is None or not _timestamp_is_fresh(stamp, after_ts, snapshot_available=snapshot_available) or not looks_like_openai_verification(message): continue
        code = extract_message_otp(message)
        if code and hashlib.sha256(code.encode("utf-8")).hexdigest() not in known:
            candidates.append((stamp, code))
    for _, code in sorted(candidates, key=lambda item: item[0], reverse=True):
        return code
    return None


def extract_assurivo_html_otp(value: str) -> str | None:
    """识别 Assurivo HTML 邮件视图中的 OpenAI 验证码。

    HTML 视图不总带邮件时间字段，不能沿用 JSON 的“新于 after_ts”筛选；因此只
    接受同时具备 OpenAI/ChatGPT 身份、验证语义和六码的同一页内容，避免把普通
    数字当 OTP。Assurivo 返回为空或没有六码时仍继续等待，不会猜测验证码。
    """
    text = re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))
    lower = text.lower()
    if not ("openai" in lower or "chatgpt" in lower):
        return None
    if not any(word in lower for word in _VERIFY_WORDS):
        return None
    code = extract_message_otp({"subject": text, "body": text})
    if code:
        return code

    # Assurivo 有时会把完整邮件 HTML 作为转义文本塞进页面。验证码可能落在
    # 条件注释节点之间，离“verification”超过 80 个字符；但页面整体已验证为
    # OpenAI 验证邮件。此时只排除 CSS 的 #RRGGBB 色值，再取正文中的六码。
    # 不能对任意页面启用这条回退，避免把普通页面数字当成 OTP。
    for match in _OTP_RE.finditer(text):
        if match.start() and text[match.start() - 1] == "#":
            continue
        return match.group(1)
    return None


def _html_otp_candidates(value: str) -> list[str]:
    """返回已按页面顺序排列的验证码；调用方不得记录其值。"""
    text = re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))
    lower = text.lower()
    if not ("openai" in lower or "chatgpt" in lower) or not any(word in lower for word in _VERIFY_WORDS):
        return []
    return [m.group(1) for m in _OTP_RE.finditer(text) if not (m.start() and text[m.start() - 1] == "#")]


_HTML_RECEIVED_AT_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?)\b")

def _assurivo_timezone():
    name = str(getattr(_email_cfg, "ASSURIVO_TIMEZONE", "Asia/Shanghai") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows 精简 Python 环境可能没有 IANA tzdata。Assurivo 默认页面时间
        # 是中国标准时间，必须继续按 +08:00 解释，不能退回 UTC 或放行历史邮件。
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        return None


def _fresh_html_otp_candidates(value: str, request_after_ts: float, known_otp_fingerprints: set[str] | None = None) -> list[str]:
    """只返回通过时间与快照指纹门槛的页面邮件码。

    纯 HH:MM:SS 没有日期/时区，无法可靠映射到本次请求，因此刻意不作为
    自动提交依据。页面没有完整 received_at 时保持 pending，由上层继续轮询。
    """
    text = re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))
    lower = text.lower()
    if not ("openai" in lower or "chatgpt" in lower) or not any(word in lower for word in _VERIFY_WORDS):
        return []
    out: list[str] = []
    parsed_times = [(m.end(), m.group(1)) for m in _HTML_RECEIVED_AT_RE.finditer(text)]
    for code_match in _OTP_RE.finditer(text):
        if code_match.start() and text[code_match.start() - 1] == "#":
            continue
        stamps = [stamp for end, stamp in parsed_times if end <= code_match.start()]
        if not stamps:
            continue
        try:
            raw_stamp = stamps[-1]
            parsed = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # 页面本地时间按显式配置解释；无有效时区时拒绝自动提交。
                tz = _assurivo_timezone()
                if tz is None:
                    continue
                parsed = parsed.replace(tzinfo=tz)
            code = code_match.group(1)
            if not _timestamp_is_fresh(parsed.timestamp(), float(request_after_ts), snapshot_available=known_otp_fingerprints is not None):
                continue
            known = known_otp_fingerprints or set()
            if hashlib.sha256(code.encode("utf-8")).hexdigest() not in known:
                out.append(code)
        except ValueError:
            continue
    return out


def capture_known_otp_fingerprints(email: str) -> set[str] | None:
    """在触发新 OTP 前取当前邮箱快照，只保留不可逆摘要，防止复用旧码。"""
    account = get_account_context(email)
    if account is None:
        return None
    try:
        response = requests.get(account.query_url, headers={"Accept": "application/json,text/html,*/*"}, timeout=int(getattr(_email_cfg, "ASSURIVO_REQUEST_TIMEOUT", 20) or 20))
        if response.status_code != 200:
            return None
        try:
            payload: Any = response.json()
        except (ValueError, json.JSONDecodeError):
            codes = _html_otp_candidates(response.text or "")
        else:
            codes = [
                code for message in _message_dicts(payload)
                if looks_like_openai_verification(message)
                for code in [extract_message_otp(message)] if code
            ]
        return {hashlib.sha256(code.encode("utf-8")).hexdigest() for code in codes}
    except requests.RequestException:
        return None


def is_empty_assurivo_html(value: str) -> bool:
    """识别 Assurivo 的空邮箱视图，和“解析器漏码”明确区分。"""
    text = re.sub(r"<[^>]*>", " ", html.unescape(str(value or ""))).lower()
    return "empty" in text and ("openai" in text or "chatgpt" in text) and any(word in text for word in _VERIFY_WORDS)


def fetch_otp_once(email: str, after_ts: float, *, diagnostics: dict | None = None, known_otp_fingerprints: set[str] | None = None) -> str | None:
    account = get_account_context(email)
    if account is None: raise AssurivoMailError(f"Assurivo 邮箱不存在: {_redact_email(email)}")
    timeout = int(getattr(_email_cfg, "ASSURIVO_REQUEST_TIMEOUT", 20) or 20)
    limit = int(getattr(_email_cfg, "ASSURIVO_RESULT_LIMIT", 20) or 20)
    try:
        # 绝不通过 params 重组查询串：完整 URL 是用户导入的资产，并且其中的
        # mail/pwd/limit 编码必须保持原样。导入阶段已确认 URL mail 与账号一致。
        response = requests.get(account.query_url, headers={"Accept": "application/json,text/html,*/*"}, timeout=timeout)
    except requests.RequestException as exc:
        raise AssurivoMailError(f"Assurivo 请求失败: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise AssurivoMailError(f"Assurivo HTTP {response.status_code}: {_redact_email(email)}")
    html_otp = None
    try:
        payload: Any = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = {"html": response.text or ""}
        if diagnostics is not None and is_empty_assurivo_html(response.text or ""):
            diagnostics["empty_html_polls"] = int(diagnostics.get("empty_html_polls", 0)) + 1
        candidates = _fresh_html_otp_candidates(response.text or "", after_ts, known_otp_fingerprints)
        html_otp = candidates[0] if candidates else None
    code = html_otp or extract_new_openai_otp(payload, after_ts, known_otp_fingerprints)
    logger.info("[Assurivo] 本次查询完成: email=%s, otp_visible=%s", _redact_email(email), bool(code))
    return code


def probe_otp_once(email: str, after_ts: float, state: AssurivoOtpState, *, diagnostics: dict | None = None, known_otp_fingerprints: set[str] | None = None, settle_seconds: int | None = None, clock: Callable[[], float] = time.monotonic) -> OTPProbeResult:
    """候选码稳定窗口：只有 60 秒内未出现更新码才允许提交。"""
    code = fetch_otp_once(email, after_ts, diagnostics=diagnostics, known_otp_fingerprints=known_otp_fingerprints)
    now = clock()
    if not code:
        logger.info("[Assurivo] OTP 轮询：候选未发现，状态=pending")
        return OTPProbeResult.pending(state)
    fingerprint = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if fingerprint != state.fingerprint:
        state.code, state.fingerprint = code, fingerprint
        seconds = max(0, int(settle_seconds if settle_seconds is not None else getattr(_email_cfg, "OTP_POST_DETECT_SETTLE", 60) or 0))
        state.ready_at_monotonic = now + seconds
        logger.info("[Assurivo] OTP 候选已发现：候选更新=True，稳定窗口剩余=%ss，状态=candidate", seconds)
        return OTPProbeResult.candidate(state, state.ready_at_monotonic)
    if state.ready_at_monotonic is not None and now >= state.ready_at_monotonic:
        logger.info("[Assurivo] OTP 候选已完成：候选更新=False，稳定窗口剩余=0s，状态=completed")
        return OTPProbeResult.completed(state.code or "", state)
    remaining = max(0, math.ceil((state.ready_at_monotonic or now) - now))
    logger.info("[Assurivo] OTP 候选已发现：候选更新=False，稳定窗口剩余=%ss，状态=candidate", remaining)
    return OTPProbeResult.candidate(state, state.ready_at_monotonic)


def fetch_latest_otp(email: str, after_ts: float, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None, retrigger: Callable[[], float] | None = None, known_otp_fingerprints: set[str] | None = None) -> str:
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), getattr(_email_cfg, "OTP_MAX_WAIT", None))
    # 没有驱动提供真实重发动作时，不能臆造 OpenAI 请求，因此只跑当前一轮。
    retries = int(getattr(_email_cfg, "OTP_RETRY_COUNT", 0) or 0) if retrigger else 0
    diagnostics: dict[str, int] = {}
    state = AssurivoOtpState()
    try:
        return wait_for_otp_with_policy(provider="assurivo", email=email, initial_after_ts=after_ts, fetch=lambda stamp: probe_otp_once(email, stamp, state, diagnostics=diagnostics, known_otp_fingerprints=known_otp_fingerprints), retrigger=retrigger or (lambda: time.time()), wait_timeout=timeout, poll_interval=int(poll_interval or getattr(_email_cfg, "OTP_POLL_INTERVAL", 5)), retry_count=retries)
    except OTPWaitExhausted as exc:
        empty_polls = int(diagnostics.get("empty_html_polls", 0))
        if empty_polls:
            exc.last_error = f"Assurivo 邮箱视图为空（{empty_polls} 次查询未返回邮件）"
        raise
