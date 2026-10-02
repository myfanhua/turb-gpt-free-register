# -*- coding: utf-8 -*-
"""
浏览器指纹与 HTTP 客户端配置。

这里集中维护同一个“浏览器环境画像”，供三层同时使用：
1. curl_cffi TLS / HTTP 头；
2. Python 端生成 Sentinel 初始 p；
3. Node VM 端运行 sdk.js。

原则：同一 BrowserSession 内稳定，不同 BrowserSession 可自然分散；协议头、JS
navigator/screen/timezone/client hints 不能互相打架。
"""
from __future__ import annotations

from config.env_loader import apply_env_overrides

import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def _latest_chrome_major(default: str = "146") -> str:
    """兼容旧模块导入；必须与 curl_cffi 实际 TLS impersonate 版本一致。"""
    return default


CHROME_MAJOR = "146"
CHROME_FULL_VERSION = "146.0.0.0"

SAFARI_VERSION = ""
SAFARI_WEBKIT_VERSION = "537.36"
MAC_OS_UA_VERSION = "10_15_7"

# ---------- curl_cffi 模拟浏览器 ----------
# curl_cffi 0.15 当前最高内置到 chrome146。UA、Client Hints、JS navigator
# 必须同步为 146；不能出现 TLS=146、HTTP/JS=149 的跨版本拼接指纹。
IMPERSONATE = "chrome146"

# ---------- 桌面 Chrome 画像 ----------
BROWSER_FAMILY = "chrome"
BROWSER_OS = "macOS"
# OS 相关字段必须和 UA / Client Hints / JS navigator 三方一致。
NAVIGATOR_PLATFORM = "MacIntel"
NAVIGATOR_VENDOR = "Google Inc."
USER_AGENT_DATA_PLATFORM = "macOS"
USER_AGENT = (
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X {MAC_OS_UA_VERSION}) "
    f"AppleWebKit/{SAFARI_WEBKIT_VERSION} (KHTML, like Gecko) "
    f"Chrome/{CHROME_FULL_VERSION} Safari/{SAFARI_WEBKIT_VERSION}"
)

# 必须与 curl_cffi chrome146 实际发出的品牌顺序和 GREASE 品牌完全一致。
# 旧值使用 HAR 中 Chrome149 的 Google Chrome/Chromium/Not)A;Brand 顺序，
# 但 TLS impersonate 已切到 chrome146，造成网络栈与显式 Client Hints 冲突。
SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
SEC_CH_UA_FULL_VERSION_LIST = '"Chromium";v="146.0.0.0", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.0.0"'
SEC_CH_UA_PLATFORM = '"macOS"'
SEC_CH_UA_PLATFORM_VERSION = '"15.7.0"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_ARCH = '"arm"'
SEC_CH_UA_BITNESS = '"64"'
SEC_CH_UA_MODEL = '""'
SEND_CLIENT_HINTS = True
SEND_HIGH_ENTROPY_CLIENT_HINTS = False

# ---------- 语言 / 时区 ----------
BROWSER_LOCALE_PROFILE = "jp"
AUTO_BROWSER_LOCALE_FROM_IP = True
IP_GEO_TIMEOUT = 6.0
IP_GEO_ENDPOINTS = [
    "https://ipinfo.io/json",
    "https://ipapi.co/json",
    "https://ipwho.is/",
]

# 代理出口质量诊断：默认不拦截，只在手动开启时拒绝云厂商/DC ASN。
# 用户可能明确使用固定云出口复现实验抓包，因此默认 False。
REJECT_CLOUD_PROXY = False
CLOUD_PROXY_ORG_KEYWORDS = [
    "amazon", "aws", "google cloud", "google llc", "microsoft", "azure",
    "digitalocean", "linode", "akamai", "ovh", "hetzner", "oracle",
    "tencent", "alibaba", "aliyun", "huawei cloud", "vultr", "contabo",
    "data center", "datacenter", "hosting", "host", "server", "cloud",
]

# ---------- Roxy/Cloak 浏览器省流量模式 ----------
# 默认关闭。开启后只拦截可选的图片/媒体，以及下面明确列出的统计/第三方 URL；
# 不拦截登录所需的 document、核心 script、stylesheet、xhr/fetch、websocket；
# Playwright 会放行带验证码/challenge 关键词的 URL。
# 该模式仅应用于 Roxy/Cloak，本地浏览器才需要节省带宽；Browser Use/Skyvern 云端
# 浏览器不会安装省流量拦截器。Selenium/CDP 只能按 URL 后缀拦截，若验证码异常可关闭。
BROWSER_DATA_SAVER_MODE: bool = False
# 注册已经拿到 accessToken 后，阻断不再需要的 ChatGPT 应用壳/遥测资源。
# 仅在 Roxy/Cloak 注册后阶段生效，不影响邮箱、验证码和登录页面。
BROWSER_DATA_SAVER_DEEP_MODE: bool = True
# 每行一个 Playwright resource_type。可选 image/media/font/manifest/texttrack 等；
# 默认只拦截 image、media；也可配置 stylesheet/font 等资源；Roxy 还会通过 Chromium 启动参数关闭图片加载，
# 遇到页面布局或验证码异常时可关闭模式。
BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES: list[str] = ["image", "media"]
# URL glob 级别的额外拦截。以下是资源明细中确认不参与邮箱密码注册主流程的
# RUM/广告统计资源；Google GSI 仅用于 Google 登录，不使用 Google 登录时默认拦截。
# 不要把 ChatGPT/auth.openai 的 CDN chunk 当作候选：即使某个 chunk 只有少量函数
# 被调用，也可能负责路由、表单切换或懒加载；需经过单变量 A/B 验证后才能加入规则。
# 不要把 chatgpt/openai 的核心 API 或 sentinel URL 加到这里。
# `**` 用于匹配 URL 中的任意路径；Roxy/Cloak 的 Playwright/Selenium 会读取这组规则。
BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS: list[str] = [
    "**://auth.openai.com/awe/api/v2/rum**",
    "**://chatgpt.com/awe/api/v2/rum**",
    "**://chatgpt.com/ces/statsc/flush**",
    "**://connect.facebook.net/**",
    "**://analytics.tiktok.com/**",
    "**://snap.licdn.com/**",
    "**://bat.bing.com/**",
    "**://accounts.google.com/gsi/client**",
]

# ---------- Roxy/Cloak 浏览器流量明细日志 ----------
# 默认关闭；开启后在每次注册结束时按单请求总字节降序输出资源 URL、类型、状态和大小。
# URL 查询参数值会脱敏，不保存请求/响应 body 或完整 Header 内容。
BROWSER_TRAFFIC_DETAIL_LOG: bool = False
BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES: int = 2000

# ---------- Roxy/Cloak 浏览器 JS 精确覆盖率 ----------
# 开启后通过 Chrome DevTools Protocol Profiler 记录本次会话实际执行过的
# JavaScript 函数/代码范围。只保存函数名、调用计数和 offset，不读取参数、返回值
# 或源码；Browser Use/Skyvern 云端浏览器不启用该监听；默认关闭，避免给正常注册增加额外开销。
BROWSER_JS_COVERAGE_LOG: bool = False
BROWSER_JS_COVERAGE_MAX_ENTRIES: int = 1000
COUNTRY_LOCALE_PROFILE_MAP = {
    "JP": "jp", "CN": "cn", "HK": "hk", "TW": "tw", "US": "us", "CA": "us",
    "SG": "sg", "GB": "gb", "AU": "gb", "DE": "de", "FR": "fr", "NL": "nl",
    "VN": "vn",
}

# 没有专用完整画像的出口国家，至少自动匹配浏览器语言。时区仍直接采用 IP
# 地理接口返回值；这样切换代理国家时不会退回固定的 ja-JP/Asia-Tokyo。
COUNTRY_LANGUAGE_TAG_MAP = {
    "TH": "th-TH", "ID": "id-ID", "MY": "ms-MY", "PH": "en-PH",
    "KR": "ko-KR", "IN": "en-IN", "BR": "pt-BR", "MX": "es-MX",
    "ES": "es-ES", "IT": "it-IT", "PT": "pt-PT", "PL": "pl-PL",
    "RU": "ru-RU", "TR": "tr-TR", "AE": "ar-AE", "SA": "ar-SA",
    "ZA": "en-ZA", "NZ": "en-NZ", "IE": "en-IE", "AT": "de-AT",
    "CH": "de-CH", "BE": "nl-BE", "SE": "sv-SE", "NO": "nb-NO",
    "DK": "da-DK", "FI": "fi-FI", "CZ": "cs-CZ", "RO": "ro-RO",
    "HU": "hu-HU", "GR": "el-GR", "IL": "he-IL", "UA": "uk-UA",
}

BROWSER_LOCALE_PROFILES = {
    "jp": {"navigator_language": "ja-JP", "navigator_languages": ["ja-JP"], "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Tokyo", "timezone_offset_minutes": 9 * 60, "timezone_name": "Japan Standard Time"},
    "cn": {"navigator_language": "zh-CN", "navigator_languages": ["zh-CN"], "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Shanghai", "timezone_offset_minutes": 8 * 60, "timezone_name": "China Standard Time"},
    "us": {"navigator_language": "en-US", "navigator_languages": ["en-US"], "accept_language": "en-US,en;q=0.9", "timezone_iana": "America/Los_Angeles", "timezone_offset_minutes": -7 * 60, "timezone_name": "Pacific Daylight Time"},
    "sg": {"navigator_language": "en-SG", "navigator_languages": ["en-SG"], "accept_language": "en-SG,en-US;q=0.9,en;q=0.8", "timezone_iana": "Asia/Singapore", "timezone_offset_minutes": 8 * 60, "timezone_name": "Singapore Standard Time"},
    "hk": {"navigator_language": "zh-HK", "navigator_languages": ["zh-HK"], "accept_language": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6", "timezone_iana": "Asia/Hong_Kong", "timezone_offset_minutes": 8 * 60, "timezone_name": "Hong Kong Standard Time"},
    "tw": {"navigator_language": "zh-TW", "navigator_languages": ["zh-TW"], "accept_language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Taipei", "timezone_offset_minutes": 8 * 60, "timezone_name": "Taipei Standard Time"},
    "gb": {"navigator_language": "en-GB", "navigator_languages": ["en-GB"], "accept_language": "en-GB,en-US;q=0.9,en;q=0.8", "timezone_iana": "Europe/London", "timezone_offset_minutes": 1 * 60, "timezone_name": "British Summer Time"},
    "de": {"navigator_language": "de-DE", "navigator_languages": ["de-DE"], "accept_language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Berlin", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "fr": {"navigator_language": "fr-FR", "navigator_languages": ["fr-FR"], "accept_language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Paris", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "nl": {"navigator_language": "nl-NL", "navigator_languages": ["nl-NL"], "accept_language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Amsterdam", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "vn": {"navigator_language": "vi-VN", "navigator_languages": ["vi-VN", "vi", "en-US", "en"], "accept_language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Ho_Chi_Minh", "timezone_offset_minutes": 7 * 60, "timezone_name": "Indochina Time"},
}

TIMEZONE_NAME_BY_IANA = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Hong_Kong": "Hong Kong Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "America/Los_Angeles": "Pacific Daylight Time",
    "America/New_York": "Eastern Daylight Time",
    "America/Chicago": "Central Daylight Time",
    "America/Denver": "Mountain Daylight Time",
    "Europe/London": "British Summer Time",
    "Europe/Berlin": "Central European Summer Time",
    "Europe/Paris": "Central European Summer Time",
    "Europe/Amsterdam": "Central European Summer Time",
    "Asia/Ho_Chi_Minh": "Indochina Time",
    "Asia/Bangkok": "Indochina Time",
}


def _normalize_locale_profile_key(value: str | None = None) -> str:
    """Normalize .env/WebUI profile values and keep a valid fallback."""
    configured = str(BROWSER_LOCALE_PROFILE if value is None else value or "").strip().lower()
    return configured if configured in BROWSER_LOCALE_PROFILES else "jp"


def _offset_minutes_for_timezone(tz_name: str, default: int) -> int:
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
        if offset is not None:
            return int(offset.total_seconds() // 60)
    except Exception:
        pass
    return int(default)


def _locale_profile_key_from_geo(geo: dict | None) -> str:
    default_profile = _normalize_locale_profile_key()
    if not geo or not AUTO_BROWSER_LOCALE_FROM_IP:
        return default_profile
    country = str(geo.get("country") or geo.get("country_code") or "").upper()
    return COUNTRY_LOCALE_PROFILE_MAP.get(country, default_profile)


def _build_locale_from_geo(geo: dict | None) -> dict:
    key = _locale_profile_key_from_geo(geo)
    resolved_profile = key
    default_profile = _normalize_locale_profile_key()
    locale = dict(BROWSER_LOCALE_PROFILES.get(key, BROWSER_LOCALE_PROFILES[default_profile]))
    if geo and AUTO_BROWSER_LOCALE_FROM_IP:
        country = str(geo.get("country") or geo.get("country_code") or "").upper()
        # 专用画像覆盖常见国家；其余已知国家动态生成语言字段。若地理接口
        # 返回了未知国家，也使用中性的 en-US，而不是泄漏本机默认日语画像。
        if country not in COUNTRY_LOCALE_PROFILE_MAP:
            language_tag = COUNTRY_LANGUAGE_TAG_MAP.get(country, "en-US")
            resolved_profile = f"geo:{country.lower() or 'unknown'}"
            base_language = language_tag.split("-", 1)[0]
            languages = [language_tag]
            if base_language != language_tag:
                languages.append(base_language)
            if base_language != "en":
                languages.extend(["en-US", "en"])
                accept_language = f"{language_tag},{base_language};q=0.9,en-US;q=0.8,en;q=0.7"
            else:
                languages.append("en")
                accept_language = f"{language_tag},en;q=0.9"
            locale.update({
                "navigator_language": language_tag,
                "navigator_languages": list(dict.fromkeys(languages)),
                "accept_language": accept_language,
            })
        tz = str(geo.get("timezone") or "").strip()
        if tz:
            locale["timezone_iana"] = tz
            locale["timezone_offset_minutes"] = _offset_minutes_for_timezone(tz, int(locale["timezone_offset_minutes"]))
            locale["timezone_name"] = TIMEZONE_NAME_BY_IANA.get(tz, locale.get("timezone_name", ""))
    locale["locale_profile"] = resolved_profile
    return locale


_LOCALE = BROWSER_LOCALE_PROFILES.get(BROWSER_LOCALE_PROFILE, BROWSER_LOCALE_PROFILES["jp"])
NAVIGATOR_LANGUAGE = _LOCALE["navigator_language"]
NAVIGATOR_LANGUAGES = list(_LOCALE["navigator_languages"])
ACCEPT_LANGUAGE = _LOCALE["accept_language"]
TIMEZONE_IANA = _LOCALE["timezone_iana"]
TIMEZONE_OFFSET_MINUTES = int(_LOCALE["timezone_offset_minutes"])
TIMEZONE_NAME = _LOCALE["timezone_name"]

# ---------- Sentinel / JS VM 环境 ----------
SCREEN_WIDTH = 1680
SCREEN_HEIGHT = 1050
HARDWARE_CONCURRENCY = 6
JS_HEAP_SIZE_LIMIT = 4395630592
DEVICE_MEMORY = 8

# 这些列表必须与 sentinel/sentinel-runner.js 的 createBrowserContext 保持一致。
NAVIGATOR_PROTO_SAMPLES = [
    "createAuctionNonce−function createAuctionNonce() { [native code] }",
    "clearOriginJoinedAdInterestGroups−function clearOriginJoinedAdInterestGroups() { [native code] }",
    "updateAdInterestGroups−function updateAdInterestGroups() { [native code] }",
    "canLoadAdAuctionFencedFrame−function canLoadAdAuctionFencedFrame() { [native code] }",
    "gpu−[object GPU]",
    "getBattery−function getBattery() { [native code] }",
    "getGamepads−function getGamepads() { [native code] }",
    "javaEnabled−function javaEnabled() { [native code] }",
    "sendBeacon−function sendBeacon() { [native code] }",
    "vibrate−function vibrate() { [native code] }",
    "login−[object NavigatorLogin]",
]
DOCUMENT_KEY_SAMPLES = [
    "currentScript", "scripts", "cookie", "URL", "documentURI", "referrer",
    "title", "characterSet", "charset", "compatMode", "contentType", "readyState",
    "visibilityState", "hidden", "hasFocus", "documentElement", "body",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "getElementsByTagName", "createElement",
]
WINDOW_KEY_SAMPLES = [
    "window", "self", "top", "parent", "frames", "navigator", "screen", "location",
    "localStorage", "sessionStorage", "history", "innerWidth", "innerHeight",
    "outerWidth", "outerHeight", "devicePixelRatio", "chrome", "performance", "crypto",
    "TextEncoder", "URL", "URLSearchParams", "AbortController",
    "locationbar", "scrollX", "scrollY", "ondevicemotion",
    "requestAnimationFrame", "queueMicrotask", "onfocus", "onblur", "onpageshow",
]

SCRIPT_SRC_SAMPLES = [
    "https://accounts.google.com/gsi/client",
    "https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload",
    "https://sentinel.openai.com/sentinel/20260810913b/sdk.js",
]

WINDOW_FEATURE_FLAGS = {
    "ai": 0,
    "InstallTrigger": 0,
    "cache": 0,
    "data": 0,
    "solana": 0,
    "dump": 0,
    # HAR 样本 p[24] 为 0；默认不暴露，必要时由画像开关启用。
    "requestIdleCallback": 0,
}

# ---------- HTTP 超时 ----------
REQUEST_TIMEOUT = 30

# HAR 参考画像：Default-all-domains-1784468371563.json 解码 p[0]/p[2]/p[16] 得出。
HAR_CAPTURE_BASE_PROFILE = {"screen_width": 1680, "screen_height": 1050, "hardware_concurrency": 6, "device_memory": 8, "js_heap_size_limit": 4395630592, "device_pixel_ratio": 2}

# 常见 macOS Chrome 桌面画像池。同一 session 内保持不变；不同 session 随机分散。
# HAR_CAPTURE_BASE_PROFILE 只是候选之一，不全局固定。
BROWSER_PROFILE_POOL = [
    HAR_CAPTURE_BASE_PROFILE,
    {"screen_width": 1440, "screen_height": 900,  "hardware_concurrency": 8,  "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1512, "screen_height": 982,  "hardware_concurrency": 8,  "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1680, "screen_height": 1050, "hardware_concurrency": 8,  "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1728, "screen_height": 1117, "hardware_concurrency": 10, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1800, "screen_height": 1169, "hardware_concurrency": 10, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 2056, "screen_height": 1329, "hardware_concurrency": 12, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
]


def build_browser_environment(geo: dict | None = None, base_profile: dict | None = None) -> dict:
    """构建完整浏览器环境画像，作为所有指纹字段的单一数据源。"""
    locale = _build_locale_from_geo(geo)
    profile = dict(base_profile or random.choice(BROWSER_PROFILE_POOL))
    profile.update({
        "locale_profile": locale.get("locale_profile", BROWSER_LOCALE_PROFILE),
        "geo": dict(geo or {}),
        "timezone_iana": locale["timezone_iana"],
        "timezone_offset_minutes": int(locale["timezone_offset_minutes"]),
        "timezone_name": locale["timezone_name"],
        "navigator_language": locale["navigator_language"],
        "navigator_languages": list(locale["navigator_languages"]),
        "accept_language": locale["accept_language"],
        "browser_family": BROWSER_FAMILY,
        "browser_os": BROWSER_OS,
        "navigator_platform": NAVIGATOR_PLATFORM,
        "navigator_vendor": NAVIGATOR_VENDOR,
        "user_agent_data_platform": USER_AGENT_DATA_PLATFORM,
        "safari_version": SAFARI_VERSION,
        "safari_webkit_version": SAFARI_WEBKIT_VERSION,
        "chrome_major": CHROME_MAJOR,
        "chrome_full_version": CHROME_FULL_VERSION,
        "user_agent": USER_AGENT,
        "send_client_hints": SEND_CLIENT_HINTS,
        "sec_ch_ua": SEC_CH_UA,
        "sec_ch_ua_platform": SEC_CH_UA_PLATFORM,
        "sec_ch_ua_platform_version": SEC_CH_UA_PLATFORM_VERSION,
        "sec_ch_ua_arch": SEC_CH_UA_ARCH,
        "sec_ch_ua_bitness": SEC_CH_UA_BITNESS,
        "sec_ch_ua_model": SEC_CH_UA_MODEL,
        "sec_ch_ua_full_version_list": SEC_CH_UA_FULL_VERSION_LIST,
        "sec_ch_ua_mobile": SEC_CH_UA_MOBILE,
        "navigator_proto_samples": list(NAVIGATOR_PROTO_SAMPLES),
        "document_key_samples": list(DOCUMENT_KEY_SAMPLES),
        "window_key_samples": list(WINDOW_KEY_SAMPLES),
        "script_src_samples": list(SCRIPT_SRC_SAMPLES),
        "window_feature_flags": dict(WINDOW_FEATURE_FLAGS),
        "build_id": __import__("config.openai_protocol", fromlist=["OPENAI_BUILD_ID"]).OPENAI_BUILD_ID,
    })
    # Sentinel VM 与 HTTP 指纹必须使用同一组 screen/window/viewport/GPU 画像。
    screen_width = int(profile.get("screen_width", 1680))
    screen_height = int(profile.get("screen_height", 1050))
    profile.setdefault("screen_avail_width", screen_width)
    profile.setdefault("screen_avail_height", max(0, screen_height - 25))
    profile.setdefault("color_depth", 24)
    profile.setdefault("outer_width", int(profile["screen_avail_width"]))
    profile.setdefault("outer_height", int(profile["screen_avail_height"]))
    profile.setdefault("viewport_width", int(profile["outer_width"]))
    profile.setdefault("viewport_height", max(0, int(profile["outer_height"]) - 87))
    cores = int(profile.get("hardware_concurrency", 8))
    chip = "Apple M2 Max" if cores >= 12 else "Apple M2 Pro" if cores >= 10 else "Apple M2"
    profile.setdefault("webgl_vendor", "Google Inc. (Apple)")
    profile.setdefault(
        "webgl_renderer",
        f"ANGLE (Apple, ANGLE Metal Renderer: {chip}, Unspecified Version)",
    )
    return profile


def pick_browser_profile(geo: dict | None = None) -> dict:
    """为一个 BrowserSession 随机挑选稳定桌面画像；HAR 尺寸只是候选之一。"""
    return build_browser_environment(geo)


def validate_browser_profile(profile: dict) -> list[str]:
    """返回画像内部矛盾点，主要用于日志/自测。"""
    issues: list[str] = []
    ua = str(profile.get("user_agent") or "")
    family = str(profile.get("browser_family") or BROWSER_FAMILY)
    if family == "safari":
        if "Version/" not in ua or "Safari/" not in ua or "Chrome/" in ua or "Chromium/" in ua:
            issues.append("Safari UA 不一致")
        if profile.get("send_client_hints"):
            issues.append("Safari 不应发送 Chromium Client Hints")
    elif f"Chrome/{profile.get('chrome_full_version')}" not in ua:
        issues.append("UA 与 chrome_full_version 不一致")
    if profile.get("browser_os") == "macOS":
        if "Macintosh; Intel Mac OS X" not in ua:
            issues.append("macOS 画像但 UA 不是 Macintosh")
        if str(profile.get("navigator_platform") or "") != "MacIntel":
            issues.append("macOS 画像但 navigator.platform 不是 MacIntel")
        if "macOS" not in str(profile.get("sec_ch_ua_platform") or ""):
            issues.append("macOS 画像但 sec-ch-ua-platform 不是 macOS")
    if not profile.get("navigator_language"):
        issues.append("navigator_language 为空")
    languages = profile.get("navigator_languages") or []
    if profile.get("navigator_language") and profile.get("navigator_language") not in languages:
        issues.append("navigator.language 不在 navigator.languages 中")
    # requestIdleCallback 是否暴露由画像决定。
    return issues

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'BROWSER_LOCALE_PROFILE': 'str', 'AUTO_BROWSER_LOCALE_FROM_IP': 'bool', 'IP_GEO_TIMEOUT': 'float', 'REJECT_CLOUD_PROXY': 'bool', 'BROWSER_DATA_SAVER_MODE': 'bool', 'BROWSER_DATA_SAVER_DEEP_MODE': 'bool', 'BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES': 'list_str_multiline', 'BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS': 'list_str_multiline', 'BROWSER_TRAFFIC_DETAIL_LOG': 'bool', 'BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES': 'int', 'BROWSER_JS_COVERAGE_LOG': 'bool', 'BROWSER_JS_COVERAGE_MAX_ENTRIES': 'int'})
BROWSER_LOCALE_PROFILE = _normalize_locale_profile_key(BROWSER_LOCALE_PROFILE)
_LOCALE = BROWSER_LOCALE_PROFILES[BROWSER_LOCALE_PROFILE]
NAVIGATOR_LANGUAGE = _LOCALE["navigator_language"]
NAVIGATOR_LANGUAGES = list(_LOCALE["navigator_languages"])
ACCEPT_LANGUAGE = _LOCALE["accept_language"]
TIMEZONE_IANA = _LOCALE["timezone_iana"]
TIMEZONE_OFFSET_MINUTES = int(_LOCALE["timezone_offset_minutes"])
TIMEZONE_NAME = _LOCALE["timezone_name"]
