# -*- coding: utf-8 -*-
"""
邮箱来源调度层。

EMAIL_SOURCE 支持单个或多个来源：
    "outlook"
    "cloudflare_domain"   # 自有域名 + QQ IMAP
    "cloudflare"          # Cloudflare Worker 临时邮箱
    "generic_api"
    "imap"
    "gptmail"
    "mailnest"
    "cloudmail"
    "remail"
    "outlook,generic_api,mailnest,cloudmail,remail"   # 按顺序兜底
    ["outlook", "generic_api", "mailnest", "cloudmail", "remail"]  # 也兼容列表写法
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

EMAIL_SOURCE_TYPES = ("outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail")
_VALID_SOURCES = EMAIL_SOURCE_TYPES
_EMAIL_SOURCE_OVERRIDE: ContextVar[str | None] = ContextVar("email_source_override", default=None)


def is_supported_email_source(source: str | None) -> bool:
    return str(source or "").strip().lower() in _VALID_SOURCES


def has_email_source_override() -> bool:
    return _EMAIL_SOURCE_OVERRIDE.get() is not None


@contextmanager
def email_source_context(source: str | None) -> Iterator[None]:
    """为当前任务覆盖邮箱来源；ContextVar 不会污染并发任务。"""
    normalized = str(source or "").strip().lower() or None
    if normalized is not None and normalized not in _VALID_SOURCES:
        raise ValueError(f"不支持的邮箱来源: {normalized}")
    token = _EMAIL_SOURCE_OVERRIDE.set(normalized)
    try:
        yield
    finally:
        _EMAIL_SOURCE_OVERRIDE.reset(token)


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


def _normalize_exclude_emails(values: Iterable[str] | str | None) -> set[str]:
    """规范化重试时不得再次领取的邮箱地址。"""
    if not values:
        return set()
    raw_values = [values] if isinstance(values, str) else values
    return {str(value or "").strip().casefold() for value in raw_values if str(value or "").strip()}


def _pick_from_source(source: str, exclude_emails: set[str] | None = None) -> str:
    exclude_emails = exclude_emails or set()
    if source == "gptmail":
        from core.gptmail_client import pick_account
        return pick_account().email
    if source == "cloudflare":
        from core.cf_temp_mail_client import pick_account
        return pick_account().email
    if source == "cloudflare_domain":
        from core.qqmail_client import pick_domain_email
        return pick_domain_email()
    if source == "generic_api":
        from core.generic_api_mail_client import pick_account
        return pick_account(exclude_emails=exclude_emails).email
    if source == "imap":
        from core.imap_mail_client import pick_account
        return pick_account(exclude_emails=exclude_emails).email
    if source == "mailnest":
        from core.mailnest_client import pick_account
        return pick_account().email
    if source == "cloudmail":
        from core.cloudmail_client import pick_account
        return pick_account().email
    if source == "remail":
        from core.remail_client import pick_account
        return pick_account().email
    from core.outlook_client import pick_account
    return pick_account(exclude_emails=exclude_emails).email


def acquire_email(exclude_emails: Iterable[str] | str | None = None) -> str:
    """领取邮箱；exclude_emails 用于重试时隔离已失败邮箱。"""
    override = _EMAIL_SOURCE_OVERRIDE.get()
    sources = [override] if override else parse_email_sources()
    excluded = _normalize_exclude_emails(exclude_emails)
    last_exc: Exception | None = None
    for source in sources:
        try:
            email = str(_pick_from_source(source, excluded) or "").strip()
            if not email:
                raise RuntimeError(f"邮箱来源 {source} 返回空地址")
            if email.casefold() in excluded:
                try:
                    release_email(email, status="available", note="重试排除邮箱，已释放")
                except Exception:
                    logger.exception("[EmailProvider] 释放排除邮箱失败: %s", email)
                raise RuntimeError(f"邮箱 {email} 在本次重试排除列表中")
            logger.info(f"[EmailProvider] 使用邮箱来源: {source}, email={email}")
            return email
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[EmailProvider] 来源 {source} 领取邮箱失败: {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(f"所有邮箱来源均领取失败: {sources}; last={last_exc}")


def acquire_email_from_source(source: str, exclude_emails: Iterable[str] | str | None = None) -> str:
    """从指定来源领取邮箱，并跳过本次重试排除地址。"""
    source = str(source or "").strip().lower()
    if source not in _VALID_SOURCES:
        raise ValueError(f"不支持的邮箱来源: {source}")
    excluded = _normalize_exclude_emails(exclude_emails)
    email = str(_pick_from_source(source, excluded) or "").strip()
    if not email:
        raise RuntimeError(f"邮箱来源 {source} 返回空地址")
    if email.casefold() in excluded:
        try:
            release_email(email, status="available", note="重试排除邮箱，已释放")
        except Exception:
            logger.exception("[EmailProvider] 释放排除邮箱失败: %s", email)
        raise RuntimeError(f"邮箱 {email} 在本次重试排除列表中")
    logger.info("[EmailProvider] 指定来源领取邮箱: source=%s, email=%s", source, email)
    return email


def acquire_email_after_input(email: str | None = None, exclude_emails: Iterable[str] | str | None = None) -> str:
    """在浏览器已找到邮箱输入框后领取邮箱。

    浏览器驱动把“找到输入框”和“领取邮箱”拆成两个阶段，避免页面加载、风控
    或入口识别失败时提前消耗邮箱。传入已有邮箱时不重复领取，兼容固定邮箱模式。
    """
    current = str(email or "").strip()
    if current:
        return current

    from config import email as _email_cfg

    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)) and not has_email_source_override():
        raise RuntimeError("页面已找到邮箱输入框，但自动取邮箱未启用且未配置 REGISTER_EMAIL")
    # 无排除列表时保持旧调用形态，兼容固定邮箱模式之外的旧包装器和测试替身。
    if exclude_emails:
        allocated = str(acquire_email(exclude_emails=exclude_emails) or "").strip()
    else:
        allocated = str(acquire_email() or "").strip()
    if not allocated:
        raise RuntimeError("邮箱服务返回了空邮箱地址")
    logger.info("[EmailProvider] 已找到邮箱输入框，开始分配邮箱: %s", allocated)
    return allocated


def resolve_email_source(email: str) -> str:
    """根据邮箱判断实际来源，已注册账号优先使用落库来源。"""
    # 已注册账号的 email_source 是注册时的最终来源。必须先读它，不能因为
    # 当前进程里恰好残留了其它邮箱池上下文，或邮箱池顺序发生变化，就把同一
    # 地址误判到另一个服务商。
    registered_source = _registered_email_source(email)
    if registered_source:
        return registered_source

    from core.gptmail_client import get_account_context as get_gptmail_context
    if get_gptmail_context(email):
        return "gptmail"
    from core.cf_temp_mail_client import get_account_context as get_cf_context
    if get_cf_context(email):
        return "cloudflare"
    from core.mailnest_client import get_account_context as get_mailnest_context
    if get_mailnest_context(email):
        return "mailnest"
    from core.cloudmail_client import get_account_context as get_cloudmail_context
    if get_cloudmail_context(email):
        return "cloudmail"
    from core.remail_client import get_account_context as get_remail_context
    if get_remail_context(email):
        return "remail"

    from core import db
    if db.get_imap_email_by_email(email):
        return "imap"
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


def _normalize_explicit_email_source(value: str | None) -> str | None:
    """规范化调用方明确指定的邮箱来源。

    已注册账号的 ``email_source`` 是注册时落库的单一来源，查活时应优先使用
    这个值，而不是重新根据当前进程的临时邮箱上下文或全局 EMAIL_SOURCE 猜测。
    这里也兼容历史数据里偶尔保存的逗号/分号分隔值，取其中第一个有效来源。
    """
    if value is None:
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    for item in raw.replace(";", ",").replace("|", ",").split(","):
        source = str(item or "").strip().strip("\"'").lower()
        if source in _VALID_SOURCES:
            return source
    return None


def _registered_email_source(email: str) -> str | None:
    """读取已注册账号落库的邮箱来源。"""
    try:
        from core import db

        account = db.get_account_by_email(email)
    except Exception:
        return None
    return _normalize_explicit_email_source((account or {}).get("email_source"))


def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    email_source: str | None = None,
    force_service: bool = False,
) -> str:
    """等待并返回该邮箱最新的 ChatGPT OTP（6 位数字字符串）。

    USE_EMAIL_SERVICE=False 时走手动验证码通道（WebUI 提交 / CLI 输入），
    不再强制要求 Outlook clientId/refreshToken。
    """
    try:
        from config import email as _email_cfg
        use_service = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)) or has_email_source_override()
    except Exception:
        use_service = True

    if not use_service and not force_service:
        from core.manual_otp import wait_for_manual_otp
        from config import email as _email_cfg
        timeout = int(max_wait if max_wait is not None else (getattr(_email_cfg, "OTP_MAX_WAIT", 180) or 180))
        job_id = None
        try:
            from core import registration_service as svc
            job_id = getattr(svc._THREAD_CTX, "job_id", None)
        except Exception:
            job_id = None
        return wait_for_manual_otp(email, timeout=timeout, job_id=job_id)

    extra_kwargs = {}
    if max_wait is not None:
        extra_kwargs["max_wait"] = max_wait
    if poll_interval is not None:
        extra_kwargs["poll_interval"] = poll_interval
    if settle_seconds is not None:
        extra_kwargs["settle_seconds"] = settle_seconds

    # 新注册任务优先采用任务级来源；没有任务覆盖时，已注册账号优先采用落库来源。
    source = (
        _normalize_explicit_email_source(email_source)
        or _EMAIL_SOURCE_OVERRIDE.get()
        or _registered_email_source(email)
        or resolve_email_source(email)
    )
    if source == "gptmail":
        from core.gptmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare":
        from core.cf_temp_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare_domain":
        from core.qqmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "generic_api":
        from core.generic_api_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "imap":
        from core.imap_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "mailnest":
        from core.mailnest_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudmail":
        from core.cloudmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "remail":
        from core.remail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    from core.outlook_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)


def email_material_line(email: str, source: str | None = None) -> str:
    """返回账号换绑后应保存的邮箱素材行。"""
    source = _normalize_explicit_email_source(source) or resolve_email_source(email)
    from core import db
    row = None
    if source == "outlook":
        row = db.get_outlook_by_email(email)
    elif source == "generic_api":
        row = db.get_generic_api_email_by_email(email)
    elif source == "imap":
        row = db.get_imap_email_by_email(email)
    if row:
        return str(row.get("copy_line") or email)
    return str(email or "")


def release_email(email: str, status: str = "available", note: str | None = None) -> str:
    """按邮箱实际来源回收状态，返回来源名。"""
    source = resolve_email_source(email)
    if source == "gptmail":
        from core.gptmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare":
        from core.cf_temp_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare_domain":
        from core.qqmail_client import release_domain_email
        release_domain_email(email, status=status, note=note)
    elif source == "generic_api":
        from core.generic_api_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "imap":
        from core.imap_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "mailnest":
        from core.mailnest_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudmail":
        from core.cloudmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "remail":
        from core.remail_client import release_account
        release_account(email, status=status, note=note)
    else:
        from core.outlook_client import release_account
        release_account(email, status=status, note=note)
    return source


def release_email_if_unconsumed(email: str, note: str | None = None) -> bool:
    """回收仍停留在 used 的任务领取，且绝不覆盖已注册/已判废状态。"""
    if not (email or "").strip():
        return False

    source = resolve_email_source(email)
    from core import db

    if source == "outlook":
        changed = db.release_unconsumed_outlook(email, note=note)
    elif source == "generic_api":
        changed = db.release_unconsumed_generic_api_email(email, note=note)
    elif source == "imap":
        changed = db.release_unconsumed_imap_email(email, note=note)
    elif source == "cloudflare_domain":
        changed = db.release_unconsumed_domain_email(email, note=note)
    else:
        # 临时邮箱不重新进入本地池，只清理进程上下文；已有本地账号时保留上下文。
        if db.get_account_by_email(email) is not None:
            return False
        release_email(email, status="available", note=note)
        changed = True

    if changed:
        logger.info("[EmailProvider] 已回收未消耗邮箱: source=%s, email=%s", source, email)
    return changed
