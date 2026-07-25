# -*- coding: utf-8 -*-
"""
QQ 邮箱 IMAP 客户端（Cloudflare 域名邮箱模式）

工作流：
    1. pick_domain_email()    生成 random@domain 域名邮箱并落库
    2. fetch_latest_otp()     通过 QQ 邮箱 IMAP 轮询取 OTP

依赖：Python 标准库（imaplib, email, ssl），无新增第三方包。
"""
import imaplib
import email as email_lib
import logging
import random
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path

from config import email as _email_cfg
from core.otp_utils import looks_like_openai_email, extract_otp
from core.otp_wait_policy import OTPProbeResult, resolve_wait_timeout, wait_for_otp_with_policy

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class QQMailClientError(RuntimeError):
    """QQ 邮箱服务相关异常。"""


@dataclass
class QQMailProbeState:
    best_otp: str | None = None
    best_ts: float = 0.0
    best_subject: str = ""
    settle_until: float | None = None


# ============================================================
# 邮件解析工具
# ============================================================

def _decode_email_header(header_value: str | None) -> str:
    """解码邮件头（处理 =?UTF-8?B?...?= 等编码）。"""
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def _parse_email_date(msg) -> float | None:
    """从 email.message 解析日期为 UTC 时间戳。"""
    date_str = msg.get("Date") or msg.get("date")
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    try:
        parsed = email_lib.utils.parsedate(date_str)
        if parsed:
            import calendar
            return calendar.timegm(parsed)
    except Exception:
        pass
    return None


def _get_msg_text(msg) -> str:
    """递归提取邮件正文（纯文本优先）。"""
    if msg.is_multipart():
        text_parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if "attachment" in cdisp:
                continue
            if ctype == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        text_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
        if text_parts:
            return "\n".join(text_parts)

        # fallback: text/html
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if "attachment" in cdisp:
                continue
            if ctype == "text/html":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
                except Exception:
                    pass
        return ""

    # not multipart
    try:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    except Exception:
        pass
    return ""


def _msg_to_dict(msg) -> dict:
    """将 email.message 转为统一 dict（与 outlook_client 兼容）。"""
    subject = _decode_email_header(msg.get("Subject") or msg.get("subject") or "")
    from_ = _decode_email_header(msg.get("From") or msg.get("from") or "")
    to_ = _decode_email_header(msg.get("To") or msg.get("to") or "")
    body_text = _get_msg_text(msg)
    ts = _parse_email_date(msg)
    ts_str = (
        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ts else ""
    )
    return {
        "subject": subject,
        "from": from_,
        "to": to_,
        "sendEmail": from_,
        "text": body_text,
        "bodyPreview": body_text,
        "bodyText": body_text,
        "date": ts_str,
        "receivedDateTime": ts_str,
    }


# ============================================================
# IMAP 连接与搜索
# ============================================================

def _connect_imap() -> imaplib.IMAP4_SSL:
    """连接 QQ 邮箱 IMAP 服务器并返回连接对象。"""
    server = _email_cfg.QQ_IMAP_SERVER
    port = _email_cfg.QQ_IMAP_PORT
    qq_email = _email_cfg.QQ_EMAIL
    password = _email_cfg.QQ_IMAP_PASSWORD

    if not qq_email or not password:
        raise QQMailClientError(
            "QQ 邮箱 IMAP 未配置，请在 config/email.py 中设置 QQ_EMAIL 和 QQ_IMAP_PASSWORD"
        )

    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(qq_email, password)
        mail.select("INBOX")
        return mail
    except imaplib.IMAP4.error as exc:
        raise QQMailClientError(f"QQ 邮箱 IMAP 登录失败: {exc}")
    except Exception as exc:
        raise QQMailClientError(f"QQ 邮箱 IMAP 连接失败: {exc}")


def _search_messages(mail: imaplib.IMAP4_SSL, after_dt: datetime | None = None) -> list[dict]:
    """搜索收件箱中 after_dt 之后的邮件，返回 dict 列表。"""
    search_criteria = "ALL"
    if after_dt is not None:
        date_str = after_dt.strftime("%d-%b-%Y")
        search_criteria = f'(SINCE {date_str})'

    status, msg_ids = mail.search(None, search_criteria)
    if status != "OK":
        logger.warning(f"[QQMail] IMAP search 失败: {status}")
        return []

    ids = msg_ids[0].split() if msg_ids[0] else []
    if not ids:
        return []

    # 只取最近 15 封（防止 inbox 太大，也够用）
    recent_ids = ids[-15:]

    messages = []
    for mid in recent_ids:
        status, data = mail.fetch(mid, "(RFC822)")
        if status != "OK":
            continue
        raw_email = data[0][1]
        try:
            msg = email_lib.message_from_bytes(raw_email)
            item = _msg_to_dict(msg)
            messages.append(item)
        except Exception as exc:
            logger.debug(f"[QQMail] 解析邮件 {mid} 失败: {exc}")
            continue

    return messages


# ============================================================
# 公共接口
# ============================================================

def pick_domain_email() -> str:
    """
    生成一个随机的域名邮箱地址并记录到 DB。
    格式：{8位随机数字母}@{EMAIL_DOMAIN}
    """
    from core.db import claim_next_domain_email

    domain = _email_cfg.EMAIL_DOMAIN
    if not domain:
        raise QQMailClientError(
            "EMAIL_DOMAIN 未配置，请在 config/email.py 中设置你的 Cloudflare 域名"
        )

    prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    email = f"{prefix}@{domain}"

    claim_next_domain_email(email)
    logger.info(f"[QQMail] 生成域名邮箱: {email}")
    return email


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    from core.db import release_domain_email as _release
    _release(email, status=status, note=note)


def fetch_otp_once(
    email: str,
    after_ts: float | None,
    state: QQMailProbeState,
    settle_seconds: int | None = None,
) -> OTPProbeResult:
    """
    通过 QQ 邮箱 IMAP 轮询取 OTP。

    每个轮询周期：
        1. 连接 QQ 邮箱 IMAP，搜索 after_ts 时间点之后的邮件
        2. 筛选 TO 收件地址匹配 email 的邮件
        3. 用 otp_utils 识别 OpenAI 验证码邮件并提取 6 位 OTP
        4. settle 机制：抓到首封后再等 OTP_SETTLE_SECONDS 秒，
           确认没有更晚的邮件才返回，避免取到途中旧 OTP

    Args:
        email: 注册用的域名邮箱地址（同时用于 IMAP TO 收件地址过滤）
        after_ts: UTC 时间戳，只看比这个时间新的邮件
        max_wait / poll_interval: 默认走 config 里的值
    """
    if after_ts is None:
        after_ts = time.time()
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    # 30s 时钟偏差容忍
    after_dt = datetime.fromtimestamp(after_ts - 30, tz=timezone.utc)

    target_lower = email.lower()
    mail = None
    try:
        mail = _connect_imap()
        messages = _search_messages(mail, after_dt=after_dt)
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass

    # 按时间降序排列
    messages.sort(key=lambda m: m.get("date") or "", reverse=True)

    # 查找最新 OpenAI 邮件
    for item in messages:
        if not looks_like_openai_email(item):
            continue

        # 必须匹配收件地址（避免捡到其他域名地址的旧验证码）
        to_field = (item.get("to") or "").lower()
        if target_lower not in to_field:
            continue

        subject = item.get("subject") or ""
        otp = extract_otp(item)
        if not otp:
            continue

        # 解析时间戳
        ts = 0.0
        raw_ts = item.get("date") or item.get("receivedDateTime") or ""
        if raw_ts:
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0

        if after_ts and ts < after_ts - 30:
            continue

        if ts > state.best_ts:
            if state.best_otp:
                logger.info(f"[QQMail] 发现更晚的 OTP={otp} (ts={raw_ts}), 替换之前的 {state.best_otp}, 重置 settle 计时")
            else:
                logger.info(f"[QQMail] 首次锁定 OTP={otp}, ts={raw_ts}, subject={subject!r}, 等 {settle}s 看是否有更晚邮件...")
            state.best_otp = otp
            state.best_ts = ts
            state.best_subject = subject
            state.settle_until = time.monotonic() + max(0, settle)
        break  # 只关心最新那一封

    now = time.monotonic()
    if state.best_otp and state.settle_until is not None and now >= state.settle_until:
        logger.info(f"[QQMail] settle 完成，返回 OTP={state.best_otp}, subject={state.best_subject!r}")
        return OTPProbeResult.completed(state.best_otp, state)
    return OTPProbeResult.candidate(state, state.settle_until) if state.best_otp else OTPProbeResult.pending(state)


def fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None) -> str:
    """按统一策略轮询 QQ IMAP 域名邮箱，返回最新 OpenAI OTP。"""
    target = str(email or "").strip()
    if not target:
        raise QQMailClientError("QQMail 取码缺少邮箱地址")
    state = QQMailProbeState()
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), _email_cfg.OTP_MAX_WAIT)
    return wait_for_otp_with_policy(provider="cloudflare_domain", email=target, initial_after_ts=after_ts if after_ts is not None else time.time(), fetch=lambda stamp: fetch_otp_once(target, stamp, state, settle_seconds), retrigger=lambda: time.time(), wait_timeout=timeout, poll_interval=poll_interval or _email_cfg.OTP_POLL_INTERVAL, retry_count=0)
