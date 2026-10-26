# -*- coding: utf-8 -*-
"""通用 IMAP 邮箱池客户端。"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import time
from core.task_control import check_stop_requested as _check_task_stop, sleep as _task_sleep
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


def pick_account() -> ImapEmailAccount:
    from core import db
    account = _account_from_row(db.claim_next_imap_email())
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


def _operation_timeout(deadline: float | None = None) -> float:
    timeout = max(0.1, float(getattr(_email_cfg, "IMAP_SOCKET_TIMEOUT", 10)))
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("IMAP 取码等待时间已耗尽")
        timeout = min(timeout, remaining)
    return timeout


def _set_timeout(mail, deadline: float | None = None) -> None:
    timeout = _operation_timeout(deadline)
    if getattr(mail, "sock", None) is not None:
        mail.sock.settimeout(timeout)


def _disconnect(mail) -> None:
    # shutdown 只关闭本地 socket；网络异常时不再阻塞等待 LOGOUT 响应。
    if mail is not None:
        try:
            mail.shutdown()
        except Exception:
            pass


def _connect(account: ImapEmailAccount, deadline: float | None = None):
    if not account.server or not account.password:
        raise ImapMailError(f"{account.email} 的 IMAP 服务器或密码为空")
    mail = None
    phase = "连接/TLS"
    try:
        cls = imaplib.IMAP4_SSL if account.use_ssl else imaplib.IMAP4
        mail = cls(account.server, account.port, timeout=_operation_timeout(deadline))
        phase = "登录"
        _set_timeout(mail, deadline)
        mail.login(account.username or account.email, account.password)
        phase = "打开目录"
        _set_timeout(mail, deadline)
        status, _ = mail.select(account.mailbox or "INBOX", readonly=True)
        if status != "OK":
            raise ImapMailError(f"无法打开邮箱目录 {account.mailbox!r}")
        return mail
    except Exception as exc:
        _disconnect(mail)
        if isinstance(exc, ImapMailError):
            raise
        raise ImapMailError(f"IMAP {phase}失败: {type(exc).__name__}: {exc}") from exc


def _search_messages(mail, after_dt: datetime | None, deadline: float | None = None) -> list[dict]:
    # IMAP 日期月份必须使用英文缩写，不能受服务器 locale 影响。
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    criteria = "ALL" if after_dt is None else (
        f'(SINCE {after_dt.day:02d}-{months[after_dt.month - 1]}-{after_dt.year})'
    )
    _set_timeout(mail, deadline)
    status, result = mail.uid("search", None, criteria)
    if status != "OK":
        raise ImapMailError("IMAP 搜索邮件失败")
    ids = result[0].split() if result and result[0] else []
    if not ids:
        return []
    # UID 避免邮件被删除时序号漂移；批量读取最新 20 封，减少网络往返。
    _set_timeout(mail, deadline)
    status, data = mail.uid("fetch", b",".join(ids[-20:]), "(BODY.PEEK[])")
    if status != "OK":
        raise ImapMailError("IMAP 读取邮件失败")
    messages: list[dict] = []
    for part in data or []:
        if not isinstance(part, tuple) or len(part) < 2 or not isinstance(part[1], bytes):
            continue
        try:
            messages.append(_msg_to_dict(email_lib.message_from_bytes(part[1])))
        except Exception as exc:
            logger.debug("[IMAP] 邮件解析失败: %s", exc)
    return messages


def _message_timestamp(item: dict) -> float:
    raw_ts = item.get("date") or item.get("receivedDateTime") or ""
    try:
        return datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    _check_task_stop()
    account = get_account_context(email)
    if account is None:
        raise ImapMailError(f"邮箱池中找不到 IMAP 账号: {email}")
    after_ts = float(time.time() if after_ts is None else after_ts)
    max_wait = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL)
    settle = int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS)
    deadline = time.monotonic() + max(0, max_wait)
    # 显式 0 表示宽松取最近一条，不能被当作当前时间。
    after_dt = datetime.fromtimestamp(after_ts - 30, tz=timezone.utc) if after_ts > 0 else None
    best_otp, best_ts, settle_until = None, 0.0, None
    last_error = None
    mail = None
    rounds = 0

    logger.info("[IMAP] 开始轮询 %s (%s:%s, SSL=%s, mailbox=%s, wait=%ss)",
                email, account.server, account.port, account.use_ssl, account.mailbox, max_wait)
    try:
        while time.monotonic() < deadline:
            _check_task_stop()
            if best_otp and settle_until is not None and time.monotonic() >= settle_until:
                return best_otp
            rounds += 1
            try:
                if mail is None:
                    mail = _connect(account, deadline=deadline)
                messages = _search_messages(mail, after_dt, deadline=deadline)
            except (ImapMailError, imaplib.IMAP4.error, OSError, EOFError, TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("[IMAP] 第 %s 轮取件失败，剩余 %.1fs: %s",
                               rounds, max(0, deadline - time.monotonic()), last_error)
                _disconnect(mail)
                mail = None
                messages = []

            messages.sort(key=_message_timestamp, reverse=True)
            rejected = {"recipient": 0, "sender": 0, "code": 0, "old": 0}
            for item in messages:
                recipient = " ".join(str(item.get(k) or "") for k in ("to", "deliveredTo", "xOriginalTo")).lower()
                if email.lower() not in recipient:
                    rejected["recipient"] += 1
                    continue
                if not looks_like_openai_email(item):
                    rejected["sender"] += 1
                    continue
                otp = extract_otp(item)
                if not otp:
                    rejected["code"] += 1
                    continue
                ts = _message_timestamp(item)
                if after_ts > 0 and ts and ts < after_ts - 30:
                    rejected["old"] += 1
                    continue
                if ts >= best_ts:
                    if otp != best_otp:
                        best_otp, settle_until = otp, time.monotonic() + max(0, settle)
                        logger.info("[IMAP] 锁定候选验证码，等待 %ss settle", settle)
                    best_ts = ts
                break
            logger.debug("[IMAP] 第 %s 轮读取 %s 封，过滤统计=%s", rounds, len(messages), rejected)
            if best_otp and settle_until is not None and time.monotonic() >= settle_until:
                return best_otp
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(max(1, interval), remaining)
            if settle_until is not None:
                delay = min(delay, max(0, settle_until - time.monotonic()))
            if delay > 0:
                _task_sleep(delay)
    finally:
        _disconnect(mail)

    if best_otp:
        return best_otp
    detail = f"，最后取件错误: {last_error}" if last_error else "，请检查目录、收件人和邮件时间"
    raise ImapMailError(f"等待 {email} 的 OTP 超时（>{max_wait}s）{detail}")
