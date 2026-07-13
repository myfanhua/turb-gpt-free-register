# -*- coding: utf-8 -*-
"""
邮箱来源调度层。

EMAIL_SOURCE 支持单个或多个来源：
    "outlook"
    "cloudflare_domain"
    "generic_api"
    "gptmail"
    "outlook,generic_api"          # 按顺序兜底
    ["outlook", "generic_api"]     # 也兼容列表写法
"""
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("outlook", "generic_api", "cloudflare_domain", "gptmail")


def parse_email_sources(value=None) -> list[str]:
    """把 EMAIL_SOURCE 解析为有序来源列表，去重并过滤空值。"""
    if value is None:
        from config import email as _email_cfg
        value = _email_cfg.EMAIL_SOURCE
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = [value]

    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().strip('"\'')
        if not s:
            continue
        if s not in _VALID_SOURCES:
            logger.warning(f"[EmailProvider] 未知邮箱来源 {s!r}，已忽略")
            continue
        if s not in out:
            out.append(s)
    return out or ["outlook"]


def _pick_from_source(source: str) -> str:
    if source == "gptmail":
        from core.gptmail_client import pick_account
        return pick_account().email
    if source == "cloudflare_domain":
        from core.qqmail_client import pick_domain_email
        return pick_domain_email()
    if source == "generic_api":
        from core.generic_api_mail_client import pick_account
        return pick_account().email
    from core.outlook_client import pick_account
    return pick_account().email


def acquire_email() -> str:
    """根据 EMAIL_SOURCE 领取一个用于注册的邮箱地址；多个来源时按顺序兜底。"""
    sources = parse_email_sources()
    last_exc: Exception | None = None
    for source in sources:
        try:
            email = _pick_from_source(source)
            logger.info(f"[EmailProvider] 使用邮箱来源: {source}, email={email}")
            return email
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[EmailProvider] 来源 {source} 领取邮箱失败: {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(f"所有邮箱来源均领取失败: {sources}; last={last_exc}")


def resolve_email_source(email: str) -> str:
    """根据邮箱在各池中的归属判断实际来源。"""
    from core.gptmail_client import get_account_context as get_gptmail_context
    if get_gptmail_context(email):
        return "gptmail"

    from core import db
    if db.get_generic_api_email_by_email(email):
        return "generic_api"
    if db.get_outlook_by_email(email):
        return "outlook"
    if db._find_domain_email(db._load_domain_pool(), email):  # 内部轻量查询，仅本项目使用
        return "cloudflare_domain"
    # 兜底：如果域名匹配 EMAIL_DOMAIN，则按域名邮箱处理
    try:
        from config import email as _email_cfg
        domain = (_email_cfg.EMAIL_DOMAIN or "").lower().strip()
        if domain and domain != "-" and email.lower().endswith("@" + domain):
            return "cloudflare_domain"
    except Exception:
        pass
    return parse_email_sources()[0]


def wait_for_otp(email: str, after_ts: float) -> str:
    """等待并返回该邮箱最新的 ChatGPT OTP（6 位数字字符串）。

    USE_EMAIL_SERVICE=False 时走手动验证码通道（WebUI 提交 / CLI 输入），
    不再强制要求 Outlook clientId/refreshToken。
    """
    try:
        from config import email as _email_cfg
        use_service = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
    except Exception:
        use_service = True

    if not use_service:
        from core.manual_otp import wait_for_manual_otp
        from config import email as _email_cfg
        timeout = int(getattr(_email_cfg, "OTP_MAX_WAIT", 180) or 180)
        job_id = None
        try:
            from core import registration_service as svc
            job_id = getattr(svc._THREAD_CTX, "job_id", None)
        except Exception:
            job_id = None
        return wait_for_manual_otp(email, timeout=timeout, job_id=job_id)

    source = resolve_email_source(email)
    if source == "gptmail":
        from core.gptmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts)
    if source == "cloudflare_domain":
        from core.qqmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts)
    if source == "generic_api":
        from core.generic_api_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts)
    from core.outlook_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts)


def release_email(email: str, status: str = "available", note: str | None = None) -> str:
    """按邮箱实际来源回收状态，返回来源名。"""
    source = resolve_email_source(email)
    if source == "gptmail":
        from core.gptmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare_domain":
        from core.qqmail_client import release_domain_email
        release_domain_email(email, status=status, note=note)
    elif source == "generic_api":
        from core.generic_api_mail_client import release_account
        release_account(email, status=status, note=note)
    else:
        from core.outlook_client import release_account
        release_account(email, status=status, note=note)
    return source
