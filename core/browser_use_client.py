# -*- coding: utf-8 -*-
"""Browser Use Cloud 客户端：构建 CDP 连接并管理 Playwright 生命周期。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from config import browser_use as _cfg

logger = logging.getLogger(__name__)


@dataclass
class BrowserUseSession:
    connect_url: str
    api_key_present: bool
    proxy_country_code: str = ""
    profile_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class BrowserUseClient:
    """最小客户端：默认用官方 connect_over_cdp websocket。"""

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key if api_key is not None else getattr(_cfg, "BROWSER_USE_API_KEY", "") or "").strip()

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "BROWSER_USE_API_KEY 为空。请到 Browser Use Cloud 创建 API Key，"
                "并在 config/browser_use.py 或 WebUI 配置页填写。"
            )
        return self.api_key

    def build_connect_url(self) -> BrowserUseSession:
        api_key = self.require_api_key()
        base = str(getattr(_cfg, "BROWSER_USE_CDP_BASE", "wss://connect.browser-use.com") or "wss://connect.browser-use.com").rstrip("?&")
        query: dict[str, str] = {"apiKey": api_key}

        proxy_country = str(getattr(_cfg, "BROWSER_USE_PROXY_COUNTRY_CODE", "") or "").strip().lower()
        use_proxy = bool(getattr(_cfg, "BROWSER_USE_USE_PROXY", True))
        if use_proxy and proxy_country:
            query["proxyCountryCode"] = proxy_country

        profile_id = str(getattr(_cfg, "BROWSER_USE_PROFILE_ID", "") or "").strip()
        if profile_id:
            query["profileId"] = profile_id

        extra = dict(getattr(_cfg, "BROWSER_USE_EXTRA_QUERY", {}) or {})
        for key, value in extra.items():
            if value is None:
                continue
            text = str(value).strip()
            if text:
                query[str(key)] = text

        connect_url = f"{base}?{urlencode(query)}"
        # 日志里不要打印完整 apiKey
        safe_query = dict(query)
        if "apiKey" in safe_query:
            safe_query["apiKey"] = safe_query["apiKey"][:6] + "***"
        logger.info(
            "[BrowserUse] CDP connect params: base=%s proxyCountry=%s profileId=%s use_proxy=%s",
            base,
            proxy_country or "-",
            profile_id or "-",
            use_proxy,
        )
        logger.debug("[BrowserUse] CDP safe query=%s", safe_query)
        return BrowserUseSession(
            connect_url=connect_url,
            api_key_present=True,
            proxy_country_code=proxy_country,
            profile_id=profile_id,
            raw={"query": safe_query, "base": base},
        )

    def open_session(self) -> BrowserUseSession:
        mode = str(getattr(_cfg, "BROWSER_USE_CONNECT_MODE", "cdp_url") or "cdp_url").strip().lower()
        if mode not in ("cdp_url", "cdp", "websocket", "ws", "sdk"):
            raise RuntimeError(f"不支持的 BROWSER_USE_CONNECT_MODE={mode!r}，当前支持 cdp_url")
        # 目前 Browser Use 官方最稳的公开接入就是 CDP websocket。
        # sdk/rest create-session 接口若以后稳定，可在此扩展。
        return self.build_connect_url()
