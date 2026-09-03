"""最小化 Config（仅 browser_register.py 用到的字段）。

剥离自原 CTF-reg/config.py，去掉 card / billing / stripe / captcha 等支付相关字段，
仅保留注册阶段必需的 proxy 字段。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


def _env_bool(name: str, default: bool) -> bool:
    """Read a conventional boolean environment variable."""

    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 120_000) -> int:
    """Read a bounded integer environment variable without failing startup."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class Config:
    """ChatGPT 注册最小配置。"""
    # 出口代理 URL，例：socks5://user:pass@host:port  或  socks5://127.0.0.1:18899
    # 留 None 走系统直连
    proxy: Optional[str] = None
    browser_profile: Optional[dict[str, Any]] = None

    # ``protocol`` keeps the existing curl_cffi state machine.  ``browser``
    # switches the registration/login UI to a real headed Playwright context.
    # The browser mode is opt-in so an installation without Playwright keeps
    # the historical behaviour until the operator enables it explicitly.
    registration_mode: str = ""
    browser_headless: Optional[bool] = None
    browser_hide_window: Optional[bool] = None
    browser_channel: str = ""
    browser_engine: str = ""
    browser_profile_dir: Optional[str] = None
    browser_timeout_ms: int = 0
    browser_no_viewport: Optional[bool] = None
    browser_stealth: Optional[bool] = None
    browser_fallback_to_protocol: Optional[bool] = None
    browser_manual_intervention: Optional[bool] = None
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = False

    def __post_init__(self) -> None:
        mode = (self.registration_mode or os.getenv("REGISTRATION_MODE", "protocol")).strip().lower()
        if mode in {"headful", "headed", "playwright", "ui", "browser"}:
            mode = "browser"
        else:
            mode = "protocol"
        self.registration_mode = mode

        self.browser_headless = _coerce_bool(
            self.browser_headless,
            _env_bool("BROWSER_HEADLESS", False),
        )
        # 浏览器引擎默认：browser 模式优先 Camoufox（Firefox 反检测，对齐 FrciblyK12）
        default_engine = "camoufox" if mode == "browser" else "playwright"
        self.browser_engine = (
            self.browser_engine.strip()
            or os.getenv("BROWSER_ENGINE", default_engine).strip().lower()
            or default_engine
        )
        # Camoufox 默认有头可见；Chromium 家族默认可藏窗
        default_hide = False if self.browser_engine in {
            "camoufox", "camou", "firefox", "firefox-camoufox"
        } else True
        self.browser_hide_window = _coerce_bool(
            self.browser_hide_window,
            _env_bool("BROWSER_HIDE_WINDOW", default_hide),
        )
        default_channel = (
            "firefox"
            if self.browser_engine in {"camoufox", "camou", "firefox", "firefox-camoufox"}
            else "chromium"
        )
        self.browser_channel = (
            self.browser_channel.strip()
            or os.getenv("BROWSER_CHANNEL", default_channel).strip()
            or default_channel
        )
        if self.browser_profile_dir is None:
            configured_dir = os.getenv("BROWSER_PROFILE_DIR", "").strip()
            self.browser_profile_dir = configured_dir or None
        if not self.browser_timeout_ms:
            self.browser_timeout_ms = _env_int("BROWSER_TIMEOUT_MS", 30_000, minimum=1_000)
        else:
            self.browser_timeout_ms = min(120_000, max(1_000, int(self.browser_timeout_ms)))
        self.browser_no_viewport = _coerce_bool(
            self.browser_no_viewport,
            _env_bool("BROWSER_NO_VIEWPORT", True),
        )
        self.browser_stealth = _coerce_bool(
            self.browser_stealth,
            _env_bool("BROWSER_STEALTH", False),
        )
        self.browser_fallback_to_protocol = _coerce_bool(
            self.browser_fallback_to_protocol,
            _env_bool("BROWSER_FALLBACK_TO_PROTOCOL", False),
        )
        self.browser_manual_intervention = _coerce_bool(
            self.browser_manual_intervention,
            _env_bool("BROWSER_MANUAL_INTERVENTION", False),
        )
