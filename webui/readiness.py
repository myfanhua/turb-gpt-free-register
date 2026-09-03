# -*- coding: utf-8 -*-
"""Secret-safe, network-free readiness evaluation for the operations console."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_STATUSES = ("ready", "warning", "blocked")


def _check(
    check_id: str,
    label: str,
    status: str,
    message: str,
    target_tab: str,
    target_group: str,
) -> dict[str, str]:
    if status not in _STATUSES:
        raise ValueError(f"unsupported readiness status: {status}")
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "message": message,
        "target_tab": target_tab,
        "target_group": target_group,
    }


def _configured(settings: Mapping[str, Any], key: str) -> bool:
    marker = f"{key}_CONFIGURED"
    if marker in settings:
        return bool(settings.get(marker))
    value = settings.get(key)
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def _valid_port(value: Any) -> bool:
    try:
        return 1 <= int(value) <= 65535
    except (TypeError, ValueError):
        return False


def _auth_check(settings: Mapping[str, Any]) -> dict[str, str]:
    if settings.get("WEBUI_AUTH_DISABLED"):
        return _check(
            "webui_auth", "访问保护", "ready", "WebUI 授权已关闭（按配置）。",
            "config", "WebUI 授权",
        )
    if settings.get("WEBUI_AUTH_CONFIGURED"):
        return _check(
            "webui_auth", "访问保护", "ready", "已配置稳定的 WebUI 授权码。",
            "config", "WebUI 授权",
        )
    return _check(
        "webui_auth", "访问保护", "warning",
        "当前使用启动时生成的临时授权码；商用环境建议配置固定授权码。",
        "config", "WebUI 授权",
    )


def _storage_check(storage_writable: bool) -> dict[str, str]:
    if storage_writable:
        return _check(
            "storage", "本地存储", "ready", "任务、账号和日志目录可写。",
            "dashboard", "",
        )
    return _check(
        "storage", "本地存储", "blocked",
        "当前数据目录不可写，任务结果可能无法保存。",
        "config", "系统设置",
    )


def _driver_check(settings: Mapping[str, Any]) -> dict[str, str]:
    driver = str(settings.get("REGISTRATION_DRIVER") or "").strip().lower()
    missing: list[str] = []
    if driver == "roxy":
        if not _configured(settings, "ROXY_API_BASE"):
            missing.append("API 地址")
        # RoxyBrowser's local API accepts workspace/project requests without
        # a token on installations where local API authentication is disabled.
        # The client already adds a token when one is configured, so it is an
        # optional capability rather than a universal readiness requirement.
        has_profile = _configured(settings, "ROXY_PROFILE_ID")
        if not has_profile and not _configured(settings, "ROXY_WORKSPACE_ID"):
            missing.append("团队 ID")
        if not has_profile and not _configured(settings, "ROXY_PROJECT_ID"):
            missing.append("项目 ID")
    elif driver == "browser_use":
        if not _configured(settings, "BROWSER_USE_API_KEY"):
            missing.append("Browser Use API Key")
    elif driver == "skyvern":
        if not _configured(settings, "SKYVERN_API_KEY"):
            missing.append("Skyvern API Key")
        if not _configured(settings, "SKYVERN_API_BASE"):
            missing.append("Skyvern API 地址")
    elif driver == "cloak":
        pass
    elif driver == "protocol":
        return _check(
            "registration_driver", "注册驱动", "warning",
            "协议模式仅作为兼容路径保留，商用运行前应完成单独验证。",
            "config", "注册方式",
        )
    else:
        return _check(
            "registration_driver", "注册驱动", "blocked",
            "尚未选择受支持的注册驱动。",
            "config", "注册方式",
        )

    if missing:
        return _check(
            "registration_driver", "注册驱动", "blocked",
            f"{driver or '当前驱动'} 缺少：{'、'.join(missing)}。",
            "config", "注册方式",
        )
    names = {
        "roxy": "RoxyBrowser",
        "cloak": "CloakBrowser",
        "browser_use": "Browser Use",
        "skyvern": "Skyvern",
    }
    return _check(
        "registration_driver", "注册驱动", "ready",
        f"已选择 {names.get(driver, driver)}，本地必填项完整。",
        "config", "注册方式",
    )


def _email_check(settings: Mapping[str, Any], pool: Mapping[str, int]) -> dict[str, str]:
    if "USE_EMAIL_SERVICE" in settings and settings.get("USE_EMAIL_SERVICE") is False:
        return _check(
            "email_source", "邮箱来源", "warning",
            "当前为手动邮箱/OTP 模式，任务运行时需要人工处理验证码。",
            "config", "邮箱 / OTP",
        )

    raw = settings.get("EMAIL_SOURCE") or ""
    if isinstance(raw, (list, tuple)):
        sources = [str(item).strip().lower() for item in raw if str(item).strip()]
    else:
        sources = [item.strip().lower() for item in str(raw).split(",") if item.strip()]
    if not sources:
        return _check(
            "email_source", "邮箱来源", "blocked", "尚未选择邮箱来源。",
            "config", "邮箱 / OTP",
        )

    available = int(pool.get("available", 0) or 0)
    ready: list[str] = []
    missing: list[str] = []
    for source in sources:
        if source in {"outlook", "generic_api"}:
            if available > 0:
                ready.append(source)
            else:
                missing.append(f"{source} 可用邮箱")
        elif source == "gptmail":
            (ready if _configured(settings, "GPTMAIL_API_KEY") else missing).append(
                "GPTMail" if _configured(settings, "GPTMAIL_API_KEY") else "GPTMail API Key"
            )
        elif source == "mailnest":
            (ready if _configured(settings, "MAIL_NEST_API_KEY") else missing).append(
                "MailNest" if _configured(settings, "MAIL_NEST_API_KEY") else "MailNest API Key"
            )
        elif source == "cloudflare":
            if _configured(settings, "CLOUDFLARE_API_BASE"):
                ready.append("Cloudflare 临时邮箱")
            else:
                missing.append("Cloudflare API 地址")
        elif source == "cloudflare_domain":
            requirements = (
                _configured(settings, "EMAIL_DOMAIN"),
                _configured(settings, "QQ_EMAIL"),
                _configured(settings, "QQ_IMAP_PASSWORD"),
            )
            if all(requirements):
                ready.append("Cloudflare 域名邮箱")
            else:
                missing.append("域名邮箱/QQ IMAP 配置")
        elif source == "cloudmail":
            has_login = _configured(settings, "CLOUDMAIL_ADMIN_EMAIL") and _configured(settings, "CLOUDMAIL_PASSWORD")
            has_auth = _configured(settings, "CLOUDMAIL_AUTH_TOKEN") or has_login
            if _configured(settings, "CLOUDMAIL_API_BASE") and has_auth:
                ready.append("CloudMail")
            else:
                missing.append("CloudMail 地址和鉴权")
        else:
            missing.append(f"未知来源 {source}")

    if not ready:
        return _check(
            "email_source", "邮箱来源", "blocked",
            f"当前邮箱来源不可用，缺少：{'、'.join(missing)}。",
            "config", "邮箱 / OTP",
        )
    if missing:
        return _check(
            "email_source", "邮箱来源", "warning",
            f"可用来源：{'、'.join(ready)}；备用来源仍缺少：{'、'.join(missing)}。",
            "config", "邮箱 / OTP",
        )
    return _check(
        "email_source", "邮箱来源", "ready",
        f"已就绪：{'、'.join(ready)}。",
        "config", "邮箱 / OTP",
    )


def _proxy_check(settings: Mapping[str, Any]) -> dict[str, str]:
    provider = str(settings.get("PROXY_PROVIDER") or "manual").strip().lower()
    if provider == "manual":
        if settings.get("PROXY_POOL_CONFIGURED"):
            return _check(
                "proxy_provider", "代理来源", "ready", "手动代理池已有可用配置。",
                "config", "内置代理",
            )
        return _check(
            "proxy_provider", "代理来源", "blocked",
            "任务选择了手动代理池，但当前没有可用代理。",
            "config", "内置代理",
        )

    if provider == "cliproxy_traffic":
        required = (
            ("账号", _configured(settings, "CLIPROXY_PROXY_USERNAME")),
            ("密码", _configured(settings, "CLIPROXY_PROXY_PASSWORD")),
            ("转发主机", _configured(settings, "CLIPROXY_FORWARD_HOST")),
            ("有效端口", _valid_port(settings.get("CLIPROXY_FORWARD_PORT"))),
        )
        label = "Cliproxy"
    elif provider == "iproyal_traffic":
        required = (
            ("账号", _configured(settings, "IPROYAL_PROXY_USERNAME")),
            ("密码", _configured(settings, "IPROYAL_PROXY_PASSWORD")),
            ("转发主机", _configured(settings, "IPROYAL_FORWARD_HOST")),
            ("有效端口", _valid_port(settings.get("IPROYAL_FORWARD_PORT"))),
        )
        label = "IPRoyal"
    elif provider == "1024proxy_traffic":
        required = (
            ("账号", _configured(settings, "PROXY_1024_USERNAME")),
            ("密码", _configured(settings, "PROXY_1024_PASSWORD")),
            ("转发主机", _configured(settings, "PROXY_1024_FORWARD_HOST")),
            ("有效端口", _valid_port(settings.get("PROXY_1024_FORWARD_PORT"))),
        )
        label = "1024Proxy"
    else:
        return _check(
            "proxy_provider", "代理来源", "blocked", "选择了未知代理来源。",
            "config", "内置代理",
        )

    missing = [name for name, ok in required if not ok]
    if missing:
        return _check(
            "proxy_provider", "代理来源", "blocked",
            f"{label} 缺少：{'、'.join(missing)}。",
            "config", "内置代理",
        )
    return _check(
        "proxy_provider", "代理来源", "ready",
        f"{label} 必填项完整。",
        "config", "内置代理",
    )


def build_readiness_report(
    settings: Mapping[str, Any],
    *,
    pool: Mapping[str, int],
    storage_writable: bool,
) -> dict[str, Any]:
    """Return a fixed, secret-free readiness contract without provider I/O."""
    checks = [
        _auth_check(settings),
        _storage_check(storage_writable),
        _driver_check(settings),
        _email_check(settings, pool),
        _proxy_check(settings),
    ]
    counts = {status: sum(item["status"] == status for item in checks) for status in _STATUSES}
    overall = "blocked" if counts["blocked"] else "warning" if counts["warning"] else "ready"
    score = round((counts["ready"] / len(checks)) * 100) if checks else 0
    return {
        "status": overall,
        "score": score,
        "counts": counts,
        "checks": checks,
    }
