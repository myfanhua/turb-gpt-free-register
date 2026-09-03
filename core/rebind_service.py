# -*- coding: utf-8 -*-
"""5102 WebUI 的 ChatGPT 邮箱换绑后台队列。"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from core import db
from rebind_core.pipeline import run_rebind_email

logger = logging.getLogger(__name__)

_WORKERS = 1
_QUEUE_LIMIT = 50
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="rebind")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PROXY_PREFIXES = ("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")


@dataclass
class RebindMailbox:
    """一次换绑任务使用的邮箱及其对应的自动收码上下文。"""

    email: str
    source: str
    mail_api: str = ""
    otp_fetcher: Callable[[float | None, float], str] | None = None
    _release: Callable[[str, str | None], None] | None = None
    _released: bool = False

    def fetch_otp(self, after_ts: float | None, timeout: float) -> str:
        if self.otp_fetcher is None:
            raise ValueError(f"邮箱来源 {self.source} 没有自动取码适配")
        return str(self.otp_fetcher(after_ts, timeout) or "").strip()

    def release(self, status: str = "failed", note: str | None = None) -> None:
        if self._released:
            return
        self._released = True
        if self._release is not None:
            try:
                self._release(status, note)
            except Exception:
                logger.exception("[换绑] 释放邮箱资源失败: source=%s", self.source)


_AUTO_PROVIDER_LABELS = {
    "cloudflare": "Cloudflare 域名邮箱（自动创建）",
    "cloudflare_domain": "自有域名邮箱（QQ IMAP）",
    "cloudmail": "CloudMail 域名邮箱",
    "generic_api": "通用 API 邮箱池",
    "outlook": "Outlook 邮箱池",
}
_AUTO_PROVIDER_ORDER = ("cloudflare", "cloudflare_domain", "cloudmail", "generic_api", "outlook")


def log_path(account_id: int) -> Path:
    """按本地账号 ID 固定日志名，避免换绑后邮箱改名导致日志丢失。"""
    return _LOG_DIR / f"rebind-{int(account_id)}.log"


def is_running(account_id: int) -> bool:
    with _LOCK:
        return int(account_id) in _RUNNING


def _append_log(account_id: int, line: str, *, clear: bool = False) -> None:
    path = log_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(f"{stamp} [INFO] {line}\n")


def _result_get(result, key: str, default=None):
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _extract_registration_password(account: dict) -> str:
    extra_raw = account.get("extra_json")
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except Exception:
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    else:
        extra = {}
    return str(
        extra.get("registration_password")
        or account.get("registration_password")
        or account.get("password")
        or ""
    ).strip()


def _normalize_proxy(proxy: str | None) -> str | None:
    value = str(proxy or "").strip()
    if not value:
        return None
    if not value.lower().startswith(_PROXY_PREFIXES):
        raise ValueError("代理必须是 http(s):// 或 socks4/5:// 地址")
    parsed = urlparse(value)
    if not parsed.hostname or parsed.port is None:
        raise ValueError("代理地址缺少有效 host/port")
    return value


def _validate_email(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not _EMAIL_RE.fullmatch(text):
        raise ValueError(f"{label} 格式无效")
    return text


def _sanitize(text: object, *, secrets: tuple[str, ...] = ()) -> str:
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    return value[:1000]


def _normalize_domains(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace(";", "\n").replace(",", "\n").splitlines()
    else:
        try:
            parts = list(raw)
        except TypeError:
            parts = [raw]
    domains: list[str] = []
    for item in parts:
        if isinstance(item, dict):
            values = [item.get(key) for key in ("domain", "name", "value", "emailDomain")]
        else:
            values = [item]
        for value in values:
            domain = str(value or "").strip().lower().lstrip("@")
            if domain and "." in domain and " " not in domain and domain not in domains:
                domains.append(domain)
    return domains


def _pool_summary(fn: Callable[[], dict]) -> dict:
    try:
        value = fn()
    except Exception as exc:
        logger.debug("[换绑] 读取邮箱池摘要失败: %s", exc)
        return {"total": 0, "available": 0, "used": 0, "failed": 0}
    return value if isinstance(value, dict) else {}


def _provider_resource(source: str, email_cfg) -> dict:
    """只读取本地配置和邮箱池摘要，不创建邮箱、不调用外部服务。"""
    source = str(source or "").strip().lower()
    item = {
        "source": source,
        "label": _AUTO_PROVIDER_LABELS.get(source, source),
        "ready": False,
        "configured": False,
        "available": None,
        "domains": [],
        "mode": "",
        "message": "",
    }
    if source == "cloudflare":
        base = str(getattr(email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
        key = str(getattr(email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
        mode = str(getattr(email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
        domains = _normalize_domains(getattr(email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", []) or [])
        item.update({"configured": bool(base), "domains": domains, "mode": "自动创建 + API 取码"})
        needs_key = mode in {"x-admin-auth", "x-api-key", "bearer", "query-key"}
        # Worker 可以在未传本地域名列表时自行选择已接入域名；不能把
        # CLOUDFLARE_API_BASE + 鉴权配置误判成“未接入”。
        item["ready"] = bool(base and (not needs_key or key))
        if item["ready"]:
            item["message"] = (
                "已识别域名邮箱服务，点击后自动创建新地址并自动收取验证码"
                if domains
                else "已识别 Cloudflare 域名邮箱服务，创建时由服务端选择域名并自动收取验证码"
            )
        elif not base:
            item["message"] = "未配置 Cloudflare 邮箱 API 地址"
        elif not domains:
            item["message"] = "未配置可用域名，且当前鉴权配置未通过"
        else:
            item["message"] = "当前鉴权模式还缺少 API Key"
        return item

    if source == "cloudflare_domain":
        domain = str(getattr(email_cfg, "EMAIL_DOMAIN", "") or "").strip().lower().lstrip("@")
        qq_email = str(getattr(email_cfg, "QQ_EMAIL", "") or "").strip()
        qq_password = str(getattr(email_cfg, "QQ_IMAP_PASSWORD", "") or "").strip()
        summary = _pool_summary(db.domain_email_pool_summary)
        item.update({
            "configured": bool(domain or qq_email or qq_password),
            "available": int(summary.get("available", 0) or 0),
            "domains": [domain] if domain else [],
            "mode": "自动创建 + QQ IMAP 取码",
        })
        item["ready"] = bool(domain and qq_email and qq_password)
        item["message"] = (
            "已识别自有域名和 QQ IMAP，自动创建并收取验证码"
            if item["ready"]
            else "需要配置 EMAIL_DOMAIN、QQ_EMAIL 和 QQ_IMAP_PASSWORD"
        )
        return item

    if source == "cloudmail":
        base = str(getattr(email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
        token = str(getattr(email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
        domains = _normalize_domains(getattr(email_cfg, "CLOUDMAIL_DOMAINS", []) or [])
        auto_add = bool(getattr(email_cfg, "CLOUDMAIL_AUTO_ADD_USER", True))
        item.update({"configured": bool(base), "domains": domains, "mode": "自动创建 + API 取码"})
        item["ready"] = bool(base and (token or not auto_add))
        item["message"] = (
            "已识别 CloudMail 邮箱服务，自动创建并收取验证码"
            if item["ready"]
            else "需要配置 CloudMail API 地址和 Authorization Token"
        )
        return item

    if source == "generic_api":
        summary = _pool_summary(db.generic_api_email_pool_summary)
        available = int(summary.get("available", 0) or 0)
        item.update({"configured": available > 0, "available": available, "mode": "复用现有邮箱池"})
        item["ready"] = available > 0
        item["message"] = "已有可用通用 API 邮箱" if item["ready"] else "没有可用的通用 API 邮箱"
        return item

    if source == "outlook":
        summary = _pool_summary(db.outlook_pool_summary)
        available = int(summary.get("available", 0) or 0)
        item.update({"configured": available > 0, "available": available, "mode": "复用现有邮箱池"})
        item["ready"] = available > 0
        item["message"] = "已有可用 Outlook 邮箱" if item["ready"] else "没有可用的 Outlook 邮箱"
        return item

    item["message"] = "当前来源没有邮箱换绑自动适配"
    return item


def detect_rebind_resources() -> dict:
    """识别本地已经接入的域名邮箱/邮箱池，并给出自动换绑首选来源。"""
    from config import email as email_cfg
    from core.email_provider import parse_email_sources

    configured_sources = parse_email_sources(getattr(email_cfg, "EMAIL_SOURCE", ""))
    ordered: list[str] = []

    def add(source: str) -> None:
        if source in _AUTO_PROVIDER_ORDER and source not in ordered:
            ordered.append(source)

    # 已配置的域名邮箱优先于注册来源，避免用户每次再指定来源或收信地址。
    cloudflare = _provider_resource("cloudflare", email_cfg)
    if cloudflare["ready"]:
        add("cloudflare")
    cloudflare_domain = _provider_resource("cloudflare_domain", email_cfg)
    if cloudflare_domain["ready"]:
        add("cloudflare_domain")
    cloudmail = _provider_resource("cloudmail", email_cfg)
    if cloudmail["ready"]:
        add("cloudmail")
    for source in configured_sources:
        add(source)
    for source in _AUTO_PROVIDER_ORDER:
        add(source)

    resources = []
    for source in ordered:
        resources.append(_provider_resource(source, email_cfg))
    selected = next((item["source"] for item in resources if item.get("ready")), "")
    return {
        "ok": True,
        "selected": selected,
        "resources": resources,
        "message": (
            f"已识别：{_AUTO_PROVIDER_LABELS.get(selected, selected)}"
            if selected
            else "没有检测到可自动使用的邮箱资源"
        ),
    }


def _choose_rebind_source(provider: str | None = None) -> str:
    requested = str(provider or "auto").strip().lower() or "auto"
    detected = detect_rebind_resources()
    resources = {str(item.get("source")): item for item in detected.get("resources", [])}
    if requested in {"", "auto"}:
        requested = str(detected.get("selected") or "")
    item = resources.get(requested)
    if not item or not item.get("ready"):
        message = str((item or {}).get("message") or "没有可用的邮箱资源")
        raise ValueError(message)
    return requested


def _provider_fetcher(source: str, email: str) -> Callable[[float | None, float], str]:
    from config import email as email_cfg

    def fetch(after_ts: float | None, timeout: float) -> str:
        kwargs = {
            "after_ts": after_ts,
            "max_wait": max(5, int(float(timeout))),
            "poll_interval": max(1, int(getattr(email_cfg, "OTP_POLL_INTERVAL", 3) or 3)),
            "settle_seconds": max(0, int(getattr(email_cfg, "OTP_SETTLE_SECONDS", 5) or 5)),
        }
        if source == "cloudflare":
            from core import cf_temp_mail_client
            return cf_temp_mail_client.fetch_latest_otp(email, **kwargs)
        if source == "cloudflare_domain":
            from core import qqmail_client
            return qqmail_client.fetch_latest_otp(email, **kwargs)
        if source == "cloudmail":
            from core import cloudmail_client
            return cloudmail_client.fetch_latest_otp(email, **kwargs)
        if source == "generic_api":
            from core import generic_api_mail_client
            return generic_api_mail_client.fetch_latest_otp(email, **kwargs)
        if source == "outlook":
            from core import outlook_client
            return outlook_client.fetch_latest_otp(email, **kwargs)
        raise ValueError(f"邮箱来源 {source} 没有自动取码适配")

    return fetch


def allocate_rebind_mailbox(provider: str | None = None) -> RebindMailbox:
    """根据已识别资源自动分配换绑目标邮箱，并绑定同来源 OTP 取码。"""
    source = _choose_rebind_source(provider)
    if source == "cloudflare":
        from core import cf_temp_mail_client
        account = cf_temp_mail_client.pick_account()
        email = str(account.email).strip()
        return RebindMailbox(
            email=email,
            source=source,
            otp_fetcher=_provider_fetcher(source, email),
            _release=lambda status, note: cf_temp_mail_client.release_account(email, status=status, note=note),
        )
    if source == "cloudflare_domain":
        from core import qqmail_client
        email = str(qqmail_client.pick_domain_email()).strip()
        db.release_domain_email(email, status="used", note="邮箱换绑占用")
        return RebindMailbox(
            email=email,
            source=source,
            otp_fetcher=_provider_fetcher(source, email),
            _release=lambda status, note: qqmail_client.release_domain_email(email, status=status, note=note),
        )
    if source == "cloudmail":
        from core import cloudmail_client
        account = cloudmail_client.pick_account()
        email = str(account.email).strip()
        return RebindMailbox(
            email=email,
            source=source,
            otp_fetcher=_provider_fetcher(source, email),
            _release=lambda status, note: cloudmail_client.release_account(email, status=status, note=note),
        )
    if source == "generic_api":
        from core import generic_api_mail_client
        account = generic_api_mail_client.pick_account()
        email = str(account.email).strip()
        return RebindMailbox(
            email=email,
            source=source,
            mail_api=str(account.code_url or "").strip(),
            otp_fetcher=_provider_fetcher(source, email),
            _release=lambda status, note: generic_api_mail_client.release_account(email, status=status, note=note),
        )
    if source == "outlook":
        from core import outlook_client
        account = outlook_client.pick_account()
        email = str(account.email).strip()
        return RebindMailbox(
            email=email,
            source=source,
            otp_fetcher=_provider_fetcher(source, email),
            _release=lambda status, note: outlook_client.release_account(email, status=status, note=note),
        )
    raise ValueError(f"邮箱来源 {source} 没有自动分配适配")


def _load_bundle(bundle_path: str | Path) -> dict:
    path = Path(str(bundle_path or ""))
    if not path.is_file():
        raise ValueError("换绑成功但未找到登录 bundle")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"登录 bundle 读取失败: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError("登录 bundle 格式无效")
    return data


def _failure(account_id: int, *, old_email: str, new_email: str, code: str, message: str) -> dict:
    error = _sanitize(message)
    payload = {
        "ok": False,
        "status": "failed",
        "code": str(code or "REBIND_FAILED"),
        "error": error,
        "message": error,
        "old_email": old_email,
        "new_email": new_email,
    }
    try:
        db.update_account_rebind(account_id, payload)
    except Exception:
        logger.exception("[换绑] 写回失败状态失败: account_id=%s", account_id)
    try:
        _append_log(account_id, f"[换绑] 失败 [{payload['code']}] {error}")
    except Exception:
        pass
    return payload


def _run_rebind(
    *,
    account_id: int,
    new_email: str,
    mail_api: str,
    mailbox: RebindMailbox | None = None,
    proxy: str | None,
    mail_timeout: float,
    trigger: str,
    release_slot: bool = False,
) -> dict:
    """执行单账号换绑；真实凭据只在内存中传递，不写入 WebUI 状态字段。"""
    account_id = int(account_id)
    new_email = str(new_email or "").strip()
    if mailbox is not None:
        mailbox_email = str(mailbox.email or "").strip()
        if new_email and mailbox_email.lower() != new_email.lower():
            result = _failure(
                account_id,
                old_email="",
                new_email=new_email,
                code="MAILBOX_MISMATCH",
                message="自动邮箱资源与目标邮箱不一致",
            )
            mailbox.release("available", "自动邮箱资源与目标邮箱不一致")
            return result
        new_email = mailbox_email
        if not mail_api:
            mail_api = str(mailbox.mail_api or "").strip()
    password = ""
    totp_secret = ""
    real_proxy = None
    mailbox_status = "failed"
    with _LOCK:
        _RUNNING.add(account_id)
    try:
        # 队列 worker 通常已经由 enqueue 完成 claim；直接调用也自动建立一次任务状态，便于单元测试和脚本复用。
        if not db.mark_account_rebind_running(account_id):
            current = db.get_account(account_id)
            if not current or str(current.get("rebind_status") or "") in {"queued", "running"}:
                return _failure(
                    account_id,
                    old_email=str((current or {}).get("email") or ""),
                    new_email=new_email,
                    code="REBIND_STATE",
                    message="账号已删除或邮箱换绑状态已被占用",
                )
            if not db.claim_account_rebind(account_id, new_email=new_email, trigger=trigger):
                current = db.get_account(account_id) or {}
                return _failure(
                    account_id,
                    old_email=str(current.get("email") or ""),
                    new_email=new_email,
                    code="REBIND_STATE",
                    message="无法占用邮箱换绑状态",
                )
            if not db.mark_account_rebind_running(account_id):
                current = db.get_account(account_id) or {}
                return _failure(
                    account_id,
                    old_email=str(current.get("email") or ""),
                    new_email=new_email,
                    code="REBIND_STATE",
                    message="无法启动邮箱换绑状态",
                )

        account = db.get_account(account_id) or {}
        old_email = str(account.get("email") or "").strip()
        password = _extract_registration_password(account)
        totp_secret = str(account.get("totp_secret") or "").strip()
        if not old_email or not password or not totp_secret:
            return _failure(
                account_id,
                old_email=old_email,
                new_email=new_email,
                code="MISSING_CREDENTIALS",
                message="账号缺少当前邮箱、注册密码或 TOTP 密钥",
            )

        conflict = db.get_account_by_email(new_email)
        if conflict and int(conflict.get("id") or 0) != account_id:
            return _failure(
                account_id,
                old_email=old_email,
                new_email=new_email,
                code="EMAIL_CONFLICT",
                message="新邮箱已经绑定到其他本地账号",
            )

        real_proxy = _normalize_proxy(proxy)
        output_dir = db._PROJECT_ROOT / "accounts" / "rebind" / str(account_id)
        _append_log(
            account_id,
            f"[换绑] 开始：old_email={old_email} new_email={new_email} trigger={trigger} "
            f"proxy={'configured' if real_proxy else 'direct'}",
        )
        raw_result = run_rebind_email(
            old_email=old_email,
            password=password,
            totp_secret=totp_secret,
            new_email=new_email,
            mail_api=str(mail_api or "").strip(),
            proxy=real_proxy,
            out_dir=output_dir,
            mail_timeout=float(mail_timeout),
            otp_fetcher=mailbox.fetch_otp if mailbox is not None else None,
        )

        if not bool(_result_get(raw_result, "ok", False)):
            return _failure(
                account_id,
                old_email=old_email,
                new_email=new_email,
                code=str(_result_get(raw_result, "code", "REBIND_FAILED") or "REBIND_FAILED"),
                message=_sanitize(
                    str(_result_get(raw_result, "message", "邮箱换绑失败") or "邮箱换绑失败"),
                    secrets=(password, totp_secret, mail_api, real_proxy or ""),
                ),
            )

        mailbox_status = "used"
        bundle_path = str(_result_get(raw_result, "bundle_path", "") or "")
        bundle = _load_bundle(bundle_path)
        session_email = str(bundle.get("email") or _result_get(raw_result, "session_email", "") or "").strip()
        access_token = str(bundle.get("access_token") or "").strip()
        if session_email.lower() != new_email.lower():
            return _failure(
                account_id,
                old_email=old_email,
                new_email=new_email,
                code="RELOGIN_EMAIL_MISMATCH",
                message="重登后的邮箱与目标邮箱不一致",
            )
        if not access_token:
            return _failure(
                account_id,
                old_email=old_email,
                new_email=new_email,
                code="BUNDLE_MISSING_AT",
                message="登录 bundle 缺少 access_token",
            )

        success_payload = {
            "ok": True,
            "status": "success",
            "code": str(_result_get(raw_result, "code", "OK") or "OK"),
            "message": "邮箱换绑完成，已刷新当前 access_token",
            "old_email": old_email,
            "new_email": new_email,
            "access_token": access_token,
            "bundle_path": bundle_path,
            "user_id": bundle.get("user_id"),
            "account_id": bundle.get("account_id"),
            "expires_at": bundle.get("expires") or bundle.get("expires_at"),
            "device_id": bundle.get("device_id"),
        }
        if not db.update_account_rebind(account_id, success_payload):
            return _failure(
                account_id,
                old_email=old_email,
                new_email=new_email,
                code="DB_WRITE_FAILED",
                message="换绑完成但本地账号写回失败",
            )
        _append_log(account_id, f"[换绑] 完成：current_email={new_email} bundle={bundle_path}")
        return {
            "ok": True,
            "status": "success",
            "code": success_payload["code"],
            "message": success_payload["message"],
            "account_id": account_id,
            "old_email": old_email,
            "new_email": new_email,
            "bundle_path": bundle_path,
        }
    except ValueError as exc:
        account = db.get_account(account_id) or {}
        return _failure(
            account_id,
            old_email=str(account.get("email") or ""),
            new_email=new_email,
            code="VALIDATION_FAILED",
            message=str(exc),
        )
    except Exception as exc:
        account = db.get_account(account_id) or {}
        error = _sanitize(
            f"{type(exc).__name__}: {exc}",
            secrets=(
                str(account.get("access_token") or ""),
                password,
                totp_secret,
                mail_api,
                real_proxy or "",
            ),
        )
        logger.exception("[换绑] 后台异常: account_id=%s", account_id)
        return _failure(
            account_id,
            old_email=str(account.get("email") or ""),
            new_email=new_email,
            code="REBIND_EXCEPTION",
            message=error,
        )
    finally:
        with _LOCK:
            _RUNNING.discard(account_id)
        if mailbox is not None:
            mailbox.release(mailbox_status, f"邮箱换绑任务 {mailbox_status}")
        if release_slot:
            _QUEUE_SLOTS.release()


def enqueue_account_rebind(
    *,
    account_id: int,
    new_email: str = "",
    mail_api: str = "",
    proxy: str | None = None,
    mail_timeout: float = 120.0,
    trigger: str = "manual",
    provider: str | None = None,
) -> dict:
    """校验并把单账号邮箱换绑任务放入后台队列；无手填邮箱时自动识别资源。"""
    account_id = int(account_id)
    account = db.get_account(account_id)
    if not account:
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    old_email = str(account.get("email") or "").strip()
    if not _extract_registration_password(account):
        return {"accepted": False, "busy": False, "error": "账号缺少注册密码"}
    if not str(account.get("totp_secret") or "").strip():
        return {"accepted": False, "busy": False, "error": "账号缺少 TOTP 密钥"}

    provider = str(provider or "").strip().lower()
    mail_api = str(mail_api or "").strip()
    mailbox: RebindMailbox | None = None
    if not mail_api:
        try:
            mailbox = allocate_rebind_mailbox(provider or "auto")
            new_email = mailbox.email
            mail_api = mailbox.mail_api
            provider = mailbox.source
        except Exception as exc:
            return {
                "accepted": False,
                "busy": False,
                "error": _sanitize(f"自动识别邮箱资源失败: {type(exc).__name__}: {exc}"),
            }
    try:
        new_email = _validate_email(new_email, "新邮箱")
    except ValueError as exc:
        if mailbox is not None:
            mailbox.release("available", str(exc))
        return {"accepted": False, "busy": False, "error": str(exc)}
    if old_email.lower() == new_email.lower():
        if mailbox is not None:
            mailbox.release("available", "新邮箱与当前邮箱相同")
        return {"accepted": False, "busy": False, "error": "新邮箱不能与当前邮箱相同"}
    conflict = db.get_account_by_email(new_email)
    if conflict and int(conflict.get("id") or 0) != account_id:
        if mailbox is not None:
            mailbox.release("available", "新邮箱已被其他账号占用")
        return {"accepted": False, "busy": False, "error": "新邮箱已经绑定到其他本地账号"}
    if mailbox is None:
        parsed_mail_api = urlparse(mail_api)
        if parsed_mail_api.scheme not in {"http", "https"} or not parsed_mail_api.netloc:
            return {"accepted": False, "busy": False, "error": "收信 API 必须是 http(s) URL"}
    try:
        real_proxy = _normalize_proxy(proxy)
        mail_timeout = max(5.0, min(900.0, float(mail_timeout)))
    except (TypeError, ValueError) as exc:
        if mailbox is not None:
            mailbox.release("available", str(exc))
        return {"accepted": False, "busy": False, "error": str(exc)}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        if mailbox is not None:
            mailbox.release("available", "换绑队列已满")
        return {"accepted": False, "busy": False, "queue_full": True, "error": "换绑队列已满，请稍后重试"}
    if not db.claim_account_rebind(account_id, new_email=new_email, trigger=trigger):
        _QUEUE_SLOTS.release()
        if mailbox is not None:
            mailbox.release("available", "账号换绑状态被占用")
        return {"accepted": False, "busy": True, "error": "该账号正在换绑邮箱"}

    _append_log(account_id, f"[换绑] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_rebind,
            account_id=account_id,
            new_email=new_email,
            mail_api=mail_api,
            mailbox=mailbox,
            proxy=real_proxy,
            mail_timeout=mail_timeout,
            trigger=str(trigger or "manual"),
            release_slot=True,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        if mailbox is not None:
            mailbox.release("available", f"换绑入队失败: {type(exc).__name__}")
        return _failure(
            account_id,
            old_email=old_email,
            new_email=new_email,
            code="QUEUE_FAILED",
            message=f"换绑入队失败: {type(exc).__name__}: {exc}",
        )
    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "old_email": old_email,
        "new_email": new_email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
        "provider": provider or "manual",
        "mail_timeout": mail_timeout,
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
