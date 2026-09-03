"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests

画像策略：
- 只使用桌面 Chrome/Edge 的自洽模板
- OS / UA / Client Hints / WebGL / screen 绑定，禁止跨 OS 随机拼装
- 语言与时区可按出口地区二次绑定
"""
from __future__ import annotations

import logging
import re
import random
import threading
import secrets
from dataclasses import dataclass, replace
from typing import Optional, get_args

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 通用 UA（仅降级路径）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# 只保留近期桌面 Chromium 家族，避免 Safari/Firefox/Tor/Android 混入注册主路径
# 优先新版本（本机 curl_cffi 已支持 chrome146/145/142/136...）
_PREFERRED_CHROME = (
    "chrome146",
    "chrome145",
    "chrome142",
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome119",
    "chrome116",
    "chrome99",
)
_PREFERRED_EDGE = (
    "edge146",
    "edge145",
    "edge142",
    "edge136",
    "edge131",
    "edge101",
    "edge99",
)
# JP 注册：只用较新桌面 Chrome，贴近手动成功抓包（Chrome 151 量级）
# JP 试用资格抓包对照：优先较新 Windows Chrome；过旧 TLS/UA 更容易低信任。
_JP_MIN_CHROME_VERSION = 136


@dataclass(frozen=True)
class BrowserProfile:
    """单次注册流程内保持一致的浏览器画像。"""

    impersonate: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_platform: str
    accept_language: str
    profile_id: str = ""
    seed: str = ""
    navigator_platform: str = "Win32"
    language: str = "en-US"
    languages: tuple[str, ...] = ("en-US", "en")
    screen_width: int = 1920
    screen_height: int = 1080
    device_memory: int = 8
    hardware_concurrency: int = 8
    timezone: str = "America/New_York"
    webgl_vendor: str = "Google Inc. (Intel)"
    webgl_renderer: str = "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"

    def headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
        }
        if self.sec_ch_ua:
            headers["Sec-CH-UA"] = self.sec_ch_ua
            headers["Sec-CH-UA-Mobile"] = "?0"
        if self.sec_ch_ua_platform:
            headers["Sec-CH-UA-Platform"] = self.sec_ch_ua_platform
        return headers

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "seed": self.seed,
            "impersonate": self.impersonate,
            "user_agent": self.user_agent,
            "sec_ch_ua": self.sec_ch_ua,
            "sec_ch_ua_platform": self.sec_ch_ua_platform,
            "accept_language": self.accept_language,
            "navigator_platform": self.navigator_platform,
            "language": self.language,
            "languages": list(self.languages),
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "device_memory": self.device_memory,
            "hardware_concurrency": self.hardware_concurrency,
            "timezone": self.timezone,
            "webgl_vendor": self.webgl_vendor,
            "webgl_renderer": self.webgl_renderer,
        }


# 出口国家/地区 -> locale 配置（CF trace 的 loc 通常是 ISO 国家码）
_LOCALE_BY_COUNTRY: dict[str, dict[str, object]] = {
    "US": {
        "timezones": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
        "accept_languages": [
            "en-US,en;q=0.9",
            "en-US,en;q=0.9,es;q=0.8",
        ],
    },
    "VN": {
        "timezones": ["Asia/Ho_Chi_Minh"],
        "accept_languages": ["en-US,en;q=0.9", "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "BR": {
        "timezones": ["America/Sao_Paulo"],
        "accept_languages": ["en-US,en;q=0.9", "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "CA": {
        "timezones": ["America/Toronto", "America/Vancouver"],
        "accept_languages": ["en-CA,en;q=0.9", "en-US,en;q=0.9"],
    },
    "GB": {
        "timezones": ["Europe/London"],
        "accept_languages": ["en-GB,en;q=0.9", "en-GB,en-US;q=0.8,en;q=0.7"],
    },
    "IE": {
        "timezones": ["Europe/Dublin"],
        "accept_languages": ["en-IE,en;q=0.9", "en-GB,en;q=0.9"],
    },
    "NL": {
        "timezones": ["Europe/Amsterdam"],
        "accept_languages": ["en-US,en;q=0.9", "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "DE": {
        "timezones": ["Europe/Berlin"],
        "accept_languages": ["en-US,en;q=0.9", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "FR": {
        "timezones": ["Europe/Paris"],
        "accept_languages": ["en-US,en;q=0.9", "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "JP": {
        # 2026-08 手动抓包（有试用资格）：Accept-Language 全程 ja-JP，timezone Asia/Tokyo。
        # 协议注册若随机 en-US，容易和 JP 出口/Sentinel 语言不一致。
        "timezones": ["Asia/Tokyo"],
        "accept_languages": [
            "ja-JP",
            "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "ja,en-US;q=0.9,en;q=0.8",
        ],
    },
    "SG": {
        "timezones": ["Asia/Singapore"],
        "accept_languages": ["en-SG,en;q=0.9", "en-US,en;q=0.9"],
    },
    "AU": {
        "timezones": ["Australia/Sydney", "Australia/Melbourne"],
        "accept_languages": ["en-AU,en;q=0.9", "en-US,en;q=0.9"],
    },
    "HK": {
        "timezones": ["Asia/Hong_Kong"],
        "accept_languages": ["en-US,en;q=0.9", "zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "TW": {
        "timezones": ["Asia/Taipei"],
        "accept_languages": ["en-US,en;q=0.9", "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
    "KR": {
        "timezones": ["Asia/Seoul"],
        "accept_languages": ["en-US,en;q=0.9", "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"],
    },
}

_DEFAULT_LOCALE = {
    "timezones": ["America/New_York", "Europe/London", "Europe/Amsterdam"],
    "accept_languages": ["en-US,en;q=0.9", "en-GB,en;q=0.9"],
}

# 与 OS 绑定的硬件/GPU，禁止 Windows + Apple M2 这类矛盾
_OS_ENV: dict[str, dict[str, object]] = {
    "Windows": {
        "screens": [(1920, 1080), (1536, 864), (1440, 900), (1366, 768), (2560, 1440), (1600, 900)],
        "hardware": [(8, 8), (8, 12), (16, 16), (16, 12), (32, 16), (4, 8)],
        "webgl": [
            (
                "Google Inc. (Intel)",
                "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (Intel)",
                "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (AMD)",
                "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
        ],
    },
    "macOS": {
        "screens": [(1440, 900), (1680, 1050), (1512, 982), (1728, 1117), (2560, 1600)],
        "hardware": [(8, 8), (8, 10), (16, 10), (16, 12), (32, 12)],
        "webgl": [
            ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
            ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
            ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"),
            (
                "Google Inc. (Apple)",
                "ANGLE (Apple, ANGLE Metal Renderer: AMD Radeon Pro 5500M, Unspecified Version)",
            ),
        ],
    },
    "Linux": {
        "screens": [(1920, 1080), (1680, 1050), (2560, 1440), (1366, 768)],
        "hardware": [(8, 8), (16, 8), (16, 16), (32, 16)],
        "webgl": [
            (
                "Google Inc. (Intel)",
                "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)",
            ),
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660/PCIe/SSE2, OpenGL 4.5.0)",
            ),
            (
                "Google Inc. (AMD)",
                "ANGLE (AMD, AMD Radeon RX 580 Series (polaris10, LLVM 15.0.7), OpenGL 4.6)",
            ),
        ],
    },
}

_FALLBACK_PROFILE_TEMPLATES = [
    {
        "impersonate": "chrome136",
        "version": "136",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "platform": '"Windows"',
        "navigator_platform": "Win32",
        "os_family": "Windows",
    },
    {
        "impersonate": "chrome136",
        "version": "136",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "platform": '"macOS"',
        "navigator_platform": "MacIntel",
        "os_family": "macOS",
    },
    {
        "impersonate": "chrome131",
        "version": "131",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": '"Windows"',
        "navigator_platform": "Win32",
        "os_family": "Windows",
    },
    {
        "impersonate": "edge131",
        "version": "131",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "platform": '"Windows"',
        "navigator_platform": "Win32",
        "os_family": "Windows",
        "browser_family": "edge",
    },
]


def _supported_curl_impersonates() -> list[str]:
    if not _HAS_CFFI:
        return []
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        values = [str(x) for x in get_args(BrowserTypeLiteral) if str(x)]
    except Exception:
        values = []
    if not values:
        values = ["chrome", "edge", "safari", "firefox"]

    valid: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        try:
            CffiSession(impersonate=value)
        except Exception:
            continue
        seen.add(value)
        valid.append(value)
    return valid


def _version_from_impersonate(name: str, default: str = "136") -> str:
    try:
        from curl_cffi.requests.impersonate import REAL_TARGET_MAP

        name = str(REAL_TARGET_MAP.get(name, name) or name)
    except Exception:
        pass
    match = re.search(r"(\d+)", name)
    if not match:
        return default
    raw = match.group(1)
    return raw[:3] if len(raw) > 3 else raw


def _edge_sec_ch_ua(version: str) -> str:
    grease = random.choice(['"Not/A)Brand";v="8"', '"Not.A/Brand";v="99"', '"Not)A;Brand";v="24"'])
    brands = [f'"Microsoft Edge";v="{version}"', f'"Chromium";v="{version}"', grease]
    random.shuffle(brands)
    return ", ".join(brands)


def _sec_ch_ua(version: str) -> str:
    grease = random.choice(['"Not/A)Brand";v="8"', '"Not.A/Brand";v="99"', '"Not)A;Brand";v="24"'])
    brands = [f'"Google Chrome";v="{version}"', f'"Chromium";v="{version}"', grease]
    random.shuffle(brands)
    return ", ".join(brands)


def _pick_desktop_impersonates(supported: list[str]) -> list[str]:
    """只保留桌面 Chrome/Edge，并按偏好顺序挑选。"""
    supported_set = set(supported)
    picked: list[str] = []
    for name in list(_PREFERRED_CHROME) + list(_PREFERRED_EDGE):
        if name in supported_set and name not in picked:
            picked.append(name)

    # 若偏好列表都不可用，从本机支持项里筛桌面 chrome/edge
    if not picked:
        for name in supported:
            n = str(name)
            if any(x in n for x in ("android", "ios", "iphone", "ipad", "tor")):
                continue
            if n.startswith("chrome") or n.startswith("edge"):
                if n not in picked:
                    picked.append(n)
            if len(picked) >= 8:
                break
    return picked


def _template_for_impersonate(name: str) -> list[dict]:
    n = str(name or "").strip()
    if not n:
        return []
    # 注册主路径不使用移动端 / Firefox / Safari / Tor
    if any(x in n for x in ("android", "ios", "iphone", "ipad", "tor")):
        return []
    if n.startswith("firefox") or n.startswith("safari"):
        return []

    version = _version_from_impersonate(n)
    out: list[dict] = []

    if n.startswith("chrome"):
        # 注册主路径只保留 Windows/macOS。Linux 桌面画像与手动成功抓包差异大，且更易触发风控分支。
        for platform, nav, os_part, os_family in (
            ('"Windows"', "Win32", "Windows NT 10.0; Win64; x64", "Windows"),
            ('"macOS"', "MacIntel", "Macintosh; Intel Mac OS X 10_15_7", "macOS"),
        ):
            out.append(
                {
                    "impersonate": n,
                    "version": version,
                    "ua": f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36",
                    "platform": platform,
                    "navigator_platform": nav,
                    "os_family": os_family,
                    "browser_family": "chrome",
                }
            )
        return out

    if n.startswith("edge"):
        out.append(
            {
                "impersonate": n,
                "version": version,
                "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0",
                "platform": '"Windows"',
                "navigator_platform": "Win32",
                "os_family": "Windows",
                "browser_family": "edge",
                "sec_ch_ua": _edge_sec_ch_ua(version),
            }
        )
        return out

    return []


def _build_profile_templates() -> list[dict]:
    supported = _supported_curl_impersonates()
    desktop = _pick_desktop_impersonates(supported)
    templates: list[dict] = []
    for impersonate in desktop:
        templates.extend(_template_for_impersonate(impersonate))
    if not templates:
        templates = [dict(x) for x in _FALLBACK_PROFILE_TEMPLATES]

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for item in templates:
        key = (str(item.get("impersonate") or ""), str(item.get("platform") or ""), str(item.get("ua") or ""))
        if key in seen:
            continue
        seen.add(key)
        if "os_family" not in item:
            platform = str(item.get("platform") or "")
            if "macOS" in platform:
                item["os_family"] = "macOS"
            elif "Linux" in platform:
                item["os_family"] = "Linux"
            else:
                item["os_family"] = "Windows"
        unique.append(item)
    logger.info("curl_cffi 桌面 TLS 指纹模板池已加载: %s (supported=%s)", len(unique), len(supported))
    return unique


_PROFILE_TEMPLATES = _build_profile_templates()
_PROFILE_LOCK = threading.Lock()
_LAST_FLOW_PROFILE_KEY = ""


def _languages_from_accept(accept_language: str) -> tuple[str, ...]:
    langs: list[str] = []
    for item in str(accept_language or "").split(","):
        lang = item.split(";")[0].strip()
        if lang and lang not in langs:
            langs.append(lang)
    return tuple(langs or ["en-US", "en"])


def _os_family_from_item(item: dict) -> str:
    family = str(item.get("os_family") or "").strip()
    if family in _OS_ENV:
        return family
    platform = str(item.get("platform") or "")
    ua = str(item.get("ua") or "")
    if "macOS" in platform or "Macintosh" in ua:
        return "macOS"
    if "Linux" in platform or "Linux" in ua:
        return "Linux"
    return "Windows"


def _locale_bundle(country_code: str = "") -> dict[str, object]:
    code = (country_code or "").strip().upper()
    if code in _LOCALE_BY_COUNTRY:
        return _LOCALE_BY_COUNTRY[code]
    return _DEFAULT_LOCALE


def _templates_for_country(country_code: str = "") -> list[dict]:
    """按注册地区筛选画像模板。

    JP（试用资格抓包对照）：强制 Windows 桌面较新 Chrome，排除 macOS/Linux/过旧版本。
    其它地区仍用完整桌面池，但已不含 Linux。
    """
    code = (country_code or "").strip().upper()
    templates = list(_PROFILE_TEMPLATES)
    if code == "JP":
        windows = [
            item
            for item in templates
            if str(item.get("os_family") or "").strip() == "Windows"
            or '"Windows"' in str(item.get("platform") or "")
        ]

        def _ver_key(item: dict) -> int:
            try:
                return int(
                    str(
                        item.get("version")
                        or _version_from_impersonate(str(item.get("impersonate") or ""))
                    )
                    or 0
                )
            except Exception:
                return 0

        # 只用 Chrome 家族，且版本 >= _JP_MIN_CHROME_VERSION
        chrome = [
            item
            for item in windows
            if str(item.get("browser_family") or "chrome") == "chrome"
            and _ver_key(item) >= int(_JP_MIN_CHROME_VERSION)
        ]
        chrome.sort(key=_ver_key, reverse=True)
        if chrome:
            return chrome
        # 兜底：任意 Windows Chrome
        fallback = [
            item
            for item in windows
            if str(item.get("browser_family") or "chrome") == "chrome"
        ]
        fallback.sort(key=_ver_key, reverse=True)
        if fallback:
            return fallback
        if windows:
            return windows
    return templates


def _profile_from_template(
    item: dict,
    *,
    seed: str = "",
    profile_id: str = "",
    country_code: str = "",
) -> BrowserProfile:
    os_family = _os_family_from_item(item)
    env = _OS_ENV.get(os_family) or _OS_ENV["Windows"]
    locale = _locale_bundle(country_code)
    code = (country_code or "").strip().upper()

    accept_langs = list(locale["accept_languages"])  # type: ignore[arg-type]
    # JP：优先纯 ja-JP，与手动成功抓包一致
    if code == "JP" and accept_langs:
        accept_language = accept_langs[0]
    else:
        accept_language = random.choice(accept_langs)
    languages = _languages_from_accept(str(accept_language))
    language = languages[0] if languages else "en-US"
    screen_width, screen_height = random.choice(list(env["screens"]))  # type: ignore[arg-type]
    device_memory, hardware_concurrency = random.choice(list(env["hardware"]))  # type: ignore[arg-type]
    webgl_vendor, webgl_renderer = random.choice(list(env["webgl"]))  # type: ignore[arg-type]
    timezones = list(locale["timezones"])  # type: ignore[arg-type]
    timezone = timezones[0] if code == "JP" and timezones else random.choice(timezones)

    version = str(item.get("version") or _version_from_impersonate(str(item.get("impersonate") or "")))
    browser_family = str(item.get("browser_family") or ("edge" if str(item.get("impersonate", "")).startswith("edge") else "chrome"))
    if "sec_ch_ua" in item and item.get("sec_ch_ua") is not None:
        sec_ch_ua = str(item.get("sec_ch_ua") or "")
    elif browser_family == "edge":
        sec_ch_ua = _edge_sec_ch_ua(version)
    else:
        sec_ch_ua = _sec_ch_ua(version)

    return BrowserProfile(
        impersonate=item["impersonate"],
        user_agent=item["ua"],
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_platform=item["platform"],
        accept_language=str(accept_language),
        profile_id=profile_id or f"bp_{secrets.token_hex(10)}",
        seed=seed or secrets.token_hex(16),
        navigator_platform=item.get("navigator_platform") or "Win32",
        language=language,
        languages=languages,
        screen_width=int(screen_width),
        screen_height=int(screen_height),
        device_memory=int(device_memory),
        hardware_concurrency=int(hardware_concurrency),
        timezone=str(timezone),
        webgl_vendor=str(webgl_vendor),
        webgl_renderer=str(webgl_renderer),
    )


def random_browser_profile(country_code: str = "") -> BrowserProfile:
    """为单次注册生成一个独立但自洽的桌面浏览器画像。"""
    templates = _templates_for_country(country_code)
    return _profile_from_template(random.choice(templates), country_code=country_code)


def random_account_profile(email: str = "", country_code: str = "") -> BrowserProfile:
    """生成可持久化的账号级浏览器画像。"""
    base = (email or "").strip().lower()
    profile_id = f"acct_{secrets.token_hex(12)}"
    if base:
        profile_id = f"acct_{secrets.token_hex(6)}_{abs(hash(base)) % 100000:05d}"
    templates = _templates_for_country(country_code)
    return _profile_from_template(
        random.choice(templates),
        seed=secrets.token_hex(16),
        profile_id=profile_id,
        country_code=country_code,
    )


def align_profile_to_country(profile: BrowserProfile | dict | None, country_code: str) -> BrowserProfile | None:
    """按出口国家校正 timezone / Accept-Language。

    JP：强制 ja-JP + Asia/Tokyo；若当前是 Linux 画像则整份换成 Windows 桌面 Chrome。
    其它地区尽量保留 OS 与 TLS 不变，只改 locale。
    """
    if profile is None:
        return None
    if isinstance(profile, dict):
        base = browser_profile_from_dict(profile)
    else:
        base = profile
    if base is None:
        return None

    code = (country_code or "").strip().upper()
    if not code:
        return base

    # JP 出口：Linux 画像与手动成功路径（Windows Chrome）差太远，直接重抽 Windows。
    ua = str(base.user_agent or "")
    is_linux = "Linux" in ua and "Android" not in ua
    if code == "JP" and is_linux:
        rebuilt = random_account_profile(country_code="JP")
        return replace(
            rebuilt,
            profile_id=base.profile_id or rebuilt.profile_id,
            seed=base.seed or rebuilt.seed,
        )

    locale = _locale_bundle(code)
    accept_langs = list(locale["accept_languages"])  # type: ignore[arg-type]
    timezones = list(locale["timezones"])  # type: ignore[arg-type]
    if code == "JP" and accept_langs:
        accept_language = accept_langs[0]
        timezone = timezones[0] if timezones else "Asia/Tokyo"
    else:
        accept_language = random.choice(accept_langs)
        timezone = random.choice(timezones)
    languages = _languages_from_accept(str(accept_language))
    return replace(
        base,
        accept_language=str(accept_language),
        language=languages[0] if languages else base.language,
        languages=languages or base.languages,
        timezone=str(timezone),
    )


def browser_profile_from_dict(data: dict | None) -> BrowserProfile | None:
    if not isinstance(data, dict):
        return None
    try:
        profile_id = str(data.get("profile_id") or "") or f"acct_{secrets.token_hex(12)}"
        seed = str(data.get("seed") or "") or secrets.token_hex(16)
        ua = str(data.get("user_agent") or "").strip()
        impersonate = str(data.get("impersonate") or "chrome136").strip()
        sec_ch_ua = str(data.get("sec_ch_ua") or "").strip()
        sec_ch_ua_platform = str(data.get("sec_ch_ua_platform") or '"Windows"').strip()
        accept_language = str(data.get("accept_language") or "en-US,en;q=0.9").strip()
        if not sec_ch_ua:
            m = re.search(r"Chrome/(\d+)", ua)
            edge_m = re.search(r"Edg/(\d+)", ua)
            if edge_m:
                sec_ch_ua = _edge_sec_ch_ua(edge_m.group(1))
            elif m:
                sec_ch_ua = _sec_ch_ua(m.group(1))
            else:
                sec_ch_ua = ""
                sec_ch_ua_platform = ""
        if not ua:
            item = random.choice(_PROFILE_TEMPLATES)
            ua = item["ua"]
            impersonate = item["impersonate"]
            sec_ch_ua_platform = item["platform"]
        languages_raw = data.get("languages")
        if isinstance(languages_raw, list):
            languages = tuple(str(x) for x in languages_raw if str(x).strip())
        else:
            languages = _languages_from_accept(accept_language)

        # 恢复时若 WebGL 与 UA OS 明显冲突，按 OS 纠正
        os_family = "Windows"
        if "Macintosh" in ua or "Mac OS" in ua:
            os_family = "macOS"
        elif "Linux" in ua and "Android" not in ua:
            os_family = "Linux"
        env = _OS_ENV[os_family]
        webgl_vendor = str(data.get("webgl_vendor") or "")
        webgl_renderer = str(data.get("webgl_renderer") or "")
        if os_family == "Windows" and ("Apple" in webgl_vendor or "Metal" in webgl_renderer):
            webgl_vendor, webgl_renderer = random.choice(list(env["webgl"]))  # type: ignore[arg-type]
        if os_family == "macOS" and "Direct3D" in webgl_renderer:
            webgl_vendor, webgl_renderer = random.choice(list(env["webgl"]))  # type: ignore[arg-type]
        if not webgl_vendor or not webgl_renderer:
            webgl_vendor, webgl_renderer = random.choice(list(env["webgl"]))  # type: ignore[arg-type]

        return BrowserProfile(
            impersonate=impersonate,
            user_agent=ua,
            sec_ch_ua=sec_ch_ua,
            sec_ch_ua_platform=sec_ch_ua_platform,
            accept_language=accept_language,
            profile_id=profile_id,
            seed=seed,
            navigator_platform=str(data.get("navigator_platform") or ("MacIntel" if os_family == "macOS" else "Win32")),
            language=str(data.get("language") or (languages[0] if languages else "en-US")),
            languages=languages or ("en-US", "en"),
            screen_width=int(data.get("screen_width") or 1920),
            screen_height=int(data.get("screen_height") or 1080),
            device_memory=int(data.get("device_memory") or 8),
            hardware_concurrency=int(data.get("hardware_concurrency") or 8),
            timezone=str(data.get("timezone") or "America/New_York"),
            webgl_vendor=str(webgl_vendor),
            webgl_renderer=str(webgl_renderer),
        )
    except Exception:
        return None


def browser_profiles_for_flow(
    count: int = 3,
    primary: BrowserProfile | dict | None = None,
    country_code: str = "",
) -> list[BrowserProfile]:
    """为一个注册流程生成主画像和 TLS 异常兜底画像。"""
    global _LAST_FLOW_PROFILE_KEY
    primary_profile = primary if isinstance(primary, BrowserProfile) else browser_profile_from_dict(primary)
    # 若主画像已是 JP/ja-JP，兜底也按 JP 池抽，避免 TLS 切换时落到 Linux/en-US。
    code = (country_code or "").strip().upper()
    if not code and primary_profile is not None:
        al = str(primary_profile.accept_language or "")
        tz = str(primary_profile.timezone or "")
        if al.startswith("ja") or "Tokyo" in tz:
            code = "JP"
    pool = _templates_for_country(code)
    with _PROFILE_LOCK:
        templates = random.sample(pool, k=min(max(1, count), len(pool)))
        first_key = f"{templates[0]['impersonate']}|{templates[0]['platform']}|{templates[0]['ua']}"
        primary_key = (
            f"{primary_profile.impersonate}|{primary_profile.sec_ch_ua_platform}|{primary_profile.user_agent}"
            if primary_profile
            else first_key
        )
        if len(templates) > 1 and primary_key == _LAST_FLOW_PROFILE_KEY:
            templates[0], templates[1] = templates[1], templates[0]
            primary_key = f"{templates[0]['impersonate']}|{templates[0]['platform']}|{templates[0]['ua']}"
        _LAST_FLOW_PROFILE_KEY = primary_key
    profiles = [_profile_from_template(item, country_code=code) for item in templates]
    if primary_profile:
        profiles = [primary_profile] + [p for p in profiles if p.user_agent != primary_profile.user_agent]
    return profiles[: max(1, count)]


def create_http_session(
    proxy: Optional[str] = None,
    impersonate: str = "chrome136",
    profile: BrowserProfile | None = None,
):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。
    """
    profile_headers = profile.headers() if profile else {"User-Agent": USER_AGENT}
    impersonate = profile.impersonate if profile else impersonate
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
        session.trust_env = False
        session.headers.update(profile_headers)
        if proxy:
            # curl_cffi 在 SOCKS 代理下建议使用 socks5h，让 DNS 走代理端解析。
            # 这能减少本地 DNS/链路导致的 TLS 握手异常。
            normalized_proxy = proxy
            if proxy.startswith("socks5://"):
                normalized_proxy = "socks5h://" + proxy[len("socks5://"):]
                logger.info("代理协议已标准化: socks5:// -> socks5h://")
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # 显式设置空代理，覆盖系统环境变量 (trust_env=False 对 libcurl 不够)
            session.proxies = {"https": "", "http": ""}
        return session
    else:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        if proxy:
            session.proxies = {"https": proxy, "http": proxy}
        session.headers.update(profile_headers)
        return session
