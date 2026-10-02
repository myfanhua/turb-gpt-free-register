# -*- coding: utf-8 -*-
"""通用 IMAP 邮箱池客户端。"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email
from core.qqmail_client import _msg_to_dict

logger = logging.getLogger(__name__)
_CONTEXT_CACHE: dict[str, "ImapEmailAccount"] = {}


class ImapMailError(RuntimeError):
    pass


@dataclass
class ImapEmailAccount:
    email: str
    password: str
    server: str
    port: int = 993
    username: str = ""
    use_ssl: bool = True
    mailbox: str = "INBOX"


def _account_from_row(row: dict | None) -> ImapEmailAccount | None:
    if not row:
        return None
    return ImapEmailAccount(
        email=str(row.get("email") or "").strip(),
        password=str(row.get("imap_password") or row.get("password") or ""),
        server=str(row.get("imap_server") or row.get("server") or "").strip(),
        port=int(row.get("imap_port") or row.get("port") or 993),
        username=str(row.get("imap_username") or row.get("username") or "").strip(),
        use_ssl=bool(row.get("imap_ssl", row.get("use_ssl", True))),
        mailbox=str(row.get("imap_mailbox") or getattr(_email_cfg, "IMAP_MAILBOX", "INBOX") or "INBOX"),
    )


def pick_account(exclude_emails: set[str] | None = None) -> ImapEmailAccount:
    from core import db
    account = _account_from_row(db.claim_next_imap_email(exclude_emails=exclude_emails))
    if account is None:
        raise ImapMailError("通用 IMAP 邮箱池没有可用邮箱，请先在邮箱池导入")
    _CONTEXT_CACHE[account.email.lower()] = account
    return account


def get_account_context(email: str) -> ImapEmailAccount | None:
    key = str(email or "").strip().lower()
    if not key:
        return None
    cached = _CONTEXT_CACHE.get(key)
    if cached:
        return cached
    from core import db
    account = _account_from_row(db.get_imap_email_by_email(email))
    if account:
        _CONTEXT_CACHE[key] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core import db
    db.release_imap_email(email, status=status, note=note)
    # 已注册账号后续查活仍需取码，因此只在真正回收为可用时清掉缓存。
    if status == "available":
        _CONTEXT_CACHE.pop(str(email or "").lower(), None)


def _connect(account: ImapEmailAccount):
    if not account.server or not account.password:
        raise ImapMailError(f"{account.email} 的 IMAP 服务器或密码为空")
    try:
        cls = imaplib.IMAP4_SSL if account.use_ssl else imaplib.IMAP4
        mail = cls(account.server, account.port)
        mail.login(account.username or account.email, account.password)
        status, _ = mail.select(account.mailbox or "INBOX")
        if status != "OK":
            raise ImapMailError(f"无法打开邮箱目录 {account.mailbox!r}")
        return mail
    except ImapMailError:
        raise
    except imaplib.IMAP4.error as exc:
        raise ImapMailError(f"IMAP 登录失败: {exc}") from exc
    except Exception as exc:
        raise ImapMailError(f"IMAP 连接失败: {exc}") from exc


def _search_messages(mail, after_dt: datetime) -> list[dict]:
    status, result = mail.search(None, f'(SINCE {after_dt.strftime("%d-%b-%Y")})')
    if status != "OK":
        return []
    ids = result[0].split() if result and result[0] else []
    messages: list[dict] = []
    for message_id in ids[-20:]:
        status, data = mail.fetch(message_id, "(RFC822)")
        if status != "OK" or not data:
            continue
        raw = next((part[1] for part in data if isinstance(part, tuple) and len(part) > 1), None)
        if not raw:
            continue
        try:
            messages.append(_msg_to_dict(email_lib.message_from_bytes(raw)))
        except Exception as exc:
            logger.debug("[IMAP] 邮件解析失败 id=%r: %s", message_id, exc)
    return messages


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    account = get_account_context(email)
    if account is None:
        raise ImapMailError(f"邮箱池中找不到 IMAP 账号: {email}")
    after_ts = float(after_ts or time.time())
    max_wait = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL)
    settle = int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS)
    deadline = time.time() + max_wait
    after_dt = datetime.fromtimestamp(after_ts - 30, tz=timezone.utc)
    best_otp, best_ts, settle_until = None, 0.0, None

    logger.info("[IMAP] 开始轮询 %s (%s:%s, SSL=%s)", email, account.server, account.port, account.use_ssl)
    while time.time() < deadline:
        mail = None
        try:
            mail = _connect(account)
            messages = _search_messages(mail, after_dt)
        except ImapMailError as exc:
            logger.warning("[IMAP] %s", exc)
            messages = []
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

        messages.sort(key=lambda item: item.get("date") or "", reverse=True)
        for item in messages:
            recipient = " ".join(str(item.get(k) or "") for k in ("to", "deliveredTo", "xOriginalTo")).lower()
            if email.lower() not in recipient or not looks_like_openai_email(item):
                continue
            otp = extract_otp(item)
            if not otp:
                continue
            raw_ts = item.get("date") or item.get("receivedDateTime") or ""
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0
            if ts and ts < after_ts - 30:
                continue
            if ts >= best_ts:
                if otp != best_otp:
                    best_otp, best_ts, settle_until = otp, ts, time.time() + settle
                    logger.info("[IMAP] 锁定候选 OTP=%s，等待 %ss settle", otp, settle)
            break
        if best_otp and settle_until is not None and time.time() >= settle_until:
            return best_otp
        time.sleep(max(1, interval))

    if best_otp:
        return best_otp
    raise ImapMailError(f"等待 {email} 的 OTP 超时（>{max_wait}s）")
