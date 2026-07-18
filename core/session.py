# -*- coding: utf-8 -*-
"""
curl_cffi Session 封装
统一管理 Cookie、请求头和 TLS 指纹
"""
import logging
import threading
import uuid
from curl_cffi.requests import Session

from config import (
    USER_AGENT, SEC_CH_UA, SEC_CH_UA_PLATFORM, SEC_CH_UA_MOBILE,
    SEC_CH_UA_FULL_VERSION_LIST, SEC_CH_UA_PLATFORM_VERSION, SEC_CH_UA_ARCH,
    SEC_CH_UA_BITNESS, SEC_CH_UA_MODEL, SEND_HIGH_ENTROPY_CLIENT_HINTS,
    ACCEPT_LANGUAGE, IMPERSONATE,
    REQUEST_TIMEOUT, pick_proxy, pick_browser_profile, validate_browser_profile,
)


logger = logging.getLogger(__name__)
_GEO_CACHE: dict[str, dict] = {}
_GEO_CACHE_LOCK = threading.Lock()


class BrowserSession:
    """
    模拟 Chrome 浏览器的 HTTP 会话管理器。
    使用 curl_cffi 的 impersonate 功能绕过 Cloudflare TLS 指纹检测。
    """

    def __init__(self, proxy: str = None, *, detect_exit_geo: bool = True):
        """
        初始化会话。

        Args:
            proxy: 代理地址，如 "socks5h://user:pass@host:port"。
                   不传则从 config.PROXY_POOL 随机抽一个。
                   显式传 "" 表示禁用代理。
            detect_exit_geo: 是否探测出口 IP 并自动选择语言/时区画像。
                             套餐查询等短请求可关闭，避免额外网络等待。
        """
        # proxy=None  → 从池里随机抽（默认行为）
        # proxy=""    → 禁用代理（直连）
        # proxy="..." → 使用指定代理
        if proxy is None:
            self.proxy = pick_proxy()
        else:
            self.proxy = proxy

        # 生成设备ID（oai-did），整个注册流程复用
        self.device_id = str(uuid.uuid4())

        # 生成 auth_session_logging_id
        self.auth_session_logging_id = str(uuid.uuid4())

        # Sentinel SDK 内部 sid：真实 SDK 会单独生成一个 UUID，和 oai-did 不是同一个值。
        # Python 初始 p 与 Node Runner 最终 token 都复用这个 sid，保持同一 SDK 实例语义。
        self.sentinel_sid = str(uuid.uuid4())
        self.react_listening_key = "_reactListening" + uuid.uuid4().hex[:12]

        # 创建 curl_cffi 会话
        self.session = Session(impersonate=IMPERSONATE)

        # 设置代理
        if self.proxy:
            self.session.proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }

        # 设置超时
        self.session.timeout = REQUEST_TIMEOUT

        # 先用当前代理检测出口 IP 地理信息，再为本会话挑一份稳定浏览器画像。
        # 这样 Accept-Language / navigator.language / timezone 可自动跟随出口地区。
        self.exit_geo = self._detect_exit_geo() if detect_exit_geo else {}
        self.browser_profile = pick_browser_profile(self.exit_geo)
        self.browser_profile["react_listening_key"] = self.react_listening_key
        issues = validate_browser_profile(self.browser_profile)
        if issues:
            logger.warning("[指纹] 浏览器画像存在不一致: %s", "; ".join(issues))

        # 让 HTTP Cookie、OAuth 参数 ext-oai-did、Sentinel 里的 id 三者一致。
        # 浏览器里 oai-did 通常会作为一方 Cookie 存在；协议层主动补齐可减少同一会话内
        # “头部/参数/JS 指纹有设备 ID，但 Cookie Jar 为空”的不一致。
        for domain in ("chatgpt.com", "auth.openai.com", "sentinel.openai.com"):
            self.session.cookies.set("oai-did", self.device_id, domain=domain, path="/")

    def _detect_exit_geo(self) -> dict:
        """通过当前代理检测出口 IP 地理信息；失败返回空 dict 并回退到默认地区画像。"""
        try:
            from config import browser as _browser_cfg
            if not getattr(_browser_cfg, "AUTO_BROWSER_LOCALE_FROM_IP", True):
                return {}
            endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
            timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
        except Exception:
            return {}

        cache_key = self.proxy or "__direct__"
        with _GEO_CACHE_LOCK:
            cached = _GEO_CACHE.get(cache_key)
            if cached is not None:
                return dict(cached)

        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        for url in endpoints:
            try:
                resp = self.session.get(url, headers=headers, timeout=timeout)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                geo = self._normalize_geo_response(data)
                if geo.get("country") or geo.get("timezone"):
                    with _GEO_CACHE_LOCK:
                        _GEO_CACHE[cache_key] = dict(geo)
                    logger.info(
                        "[指纹] 出口IP地理信息: ip=%s country=%s city=%s timezone=%s",
                        geo.get("ip") or "?", geo.get("country") or "?",
                        geo.get("city") or "?", geo.get("timezone") or "?",
                    )
                    return geo
            except Exception as exc:
                logger.debug(f"[指纹] 出口 IP 地理检测失败 endpoint={url}: {type(exc).__name__}: {exc}")
                continue
        with _GEO_CACHE_LOCK:
            _GEO_CACHE[cache_key] = {}
        return {}

    @staticmethod
    def _normalize_geo_response(data: dict) -> dict:
        """兼容 ipinfo / ipapi / ipwho.is 等常见 JSON 字段。"""
        if not isinstance(data, dict):
            return {}
        timezone = data.get("timezone")
        if isinstance(timezone, dict):
            timezone = timezone.get("id") or timezone.get("name")
        return {
            "ip": data.get("ip") or data.get("query"),
            "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
            "region": data.get("region") or data.get("regionName"),
            "city": data.get("city"),
            "timezone": timezone or "",
            "org": data.get("org") or data.get("isp") or data.get("connection", {}).get("org"),
        }

    def _get_common_headers(self) -> dict:
        """获取通用请求头，优先使用本 BrowserSession 的稳定画像。"""
        profile = getattr(self, "browser_profile", {}) or {}
        headers = {
            "User-Agent": str(profile.get("user_agent") or USER_AGENT),
            "accept-language": str(profile.get("accept_language") or ACCEPT_LANGUAGE),
        }

        # Safari 不发送 Chromium Client Hints；Chrome/Chromium 画像才补 sec-ch-*。
        send_client_hints = bool(profile.get("send_client_hints", bool(SEC_CH_UA)))
        if send_client_hints:
            if profile.get("sec_ch_ua") or SEC_CH_UA:
                headers["sec-ch-ua"] = str(profile.get("sec_ch_ua") or SEC_CH_UA)
            if profile.get("sec_ch_ua_mobile") or SEC_CH_UA_MOBILE:
                headers["sec-ch-ua-mobile"] = str(profile.get("sec_ch_ua_mobile") or SEC_CH_UA_MOBILE)
            if profile.get("sec_ch_ua_platform") or SEC_CH_UA_PLATFORM:
                headers["sec-ch-ua-platform"] = str(profile.get("sec_ch_ua_platform") or SEC_CH_UA_PLATFORM)
            if SEND_HIGH_ENTROPY_CLIENT_HINTS:
                headers.update({
                    "sec-ch-ua-full-version-list": SEC_CH_UA_FULL_VERSION_LIST,
                    "sec-ch-ua-platform-version": SEC_CH_UA_PLATFORM_VERSION,
                    "sec-ch-ua-arch": SEC_CH_UA_ARCH,
                    "sec-ch-ua-bitness": SEC_CH_UA_BITNESS,
                    "sec-ch-ua-model": SEC_CH_UA_MODEL,
                })
        return headers

    def navigator_language(self) -> str:
        """当前会话画像里的 navigator.language。"""
        return str((getattr(self, "browser_profile", {}) or {}).get("navigator_language") or "zh-CN")

    @staticmethod
    def _sec_fetch_site_for(target_origin: str, referer: str) -> str:
        """按 Referer 粗略模拟浏览器的 Sec-Fetch-Site。"""
        ref = (referer or "").lower()
        target = target_origin.lower().rstrip("/")
        if ref.startswith(target):
            return "same-origin"
        if ref.startswith("https://chatgpt.com") or ref.startswith("https://auth.openai.com") or ref.startswith("https://sentinel.openai.com"):
            return "cross-site"
        return "none"

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/login") -> dict:
        """
        获取 chatgpt.com 域名的请求头。
        用于步骤1-3。
        """
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
        })
        return headers

    def get_auth_headers(self, referer: str = "https://auth.openai.com/create-account/password") -> dict:
        """
        获取 auth.openai.com 域名的请求头。
        用于步骤7、10、12。
        """
        headers = self._get_common_headers()
        headers.update({
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
            "origin": "https://auth.openai.com",
        })
        return headers

    def get_auth_navigate_headers(self, referer: str = "https://chatgpt.com/", user_initiated: bool = True) -> dict:
        """
        获取 auth.openai.com 导航请求头（用于GET页面请求）。
        用于步骤4、5、8。
        """
        headers = self._get_common_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-site": self._sec_fetch_site_for("https://auth.openai.com", referer),
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "referer": referer,
            "priority": "u=0, i",
            "upgrade-insecure-requests": "1",
        })
        if user_initiated:
            headers["sec-fetch-user"] = "?1"
        return headers

    def get_chatgpt_navigate_headers(self, referer: str = "https://chatgpt.com/", user_initiated: bool = True) -> dict:
        """获取 chatgpt.com 页面导航请求头，用于预热登录页 / 回到应用页。"""
        headers = self._get_common_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-site": self._sec_fetch_site_for("https://chatgpt.com", referer),
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "referer": referer,
            "priority": "u=0, i",
            "upgrade-insecure-requests": "1",
        })
        if user_initiated:
            headers["sec-fetch-user"] = "?1"
        return headers

    def get_sentinel_headers(self) -> dict:
        """
        获取 sentinel.openai.com 的请求头。
        用于步骤6、9、11。
        """
        from config import SENTINEL_SV
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SV}",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "priority": "u=1, i",
        })
        return headers

    def get(self, url: str, headers: dict = None, **kwargs):
        """发送 GET 请求"""
        return self.session.get(url, headers=headers, **kwargs)

    def post(self, url: str, headers: dict = None, **kwargs):
        """发送 POST 请求"""
        return self.session.post(url, headers=headers, **kwargs)
