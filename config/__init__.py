# -*- coding: utf-8 -*-
"""
config 包的统一入口。

为保留 `from config import USER_AGENT` 这种历史用法，本文件把所有子模块的常量
重新导出到包顶层。新代码推荐按子模块直接导入：
    from config.email import EMAIL_SOURCE
    from config.proxy import pick_proxy

子模块清单：
    config.browser           浏览器指纹 / curl_cffi impersonate / HTTP 超时
    config.openai_protocol   OpenAI OAuth 固定参数 / Sentinel 版本
    config.proxy             代理池 + 随机抽取
    config.register          注册默认信息（邮箱、密码、名称、生日）
    config.email             Outlook 邮箱账号池 + OTP 轮询
    config.twofa             2FA 开关
"""

# ---------- 浏览器 / HTTP ----------
from config.browser import (
    USER_AGENT,
    CHROME_MAJOR,
    CHROME_FULL_VERSION,
    SEC_CH_UA,
    SEC_CH_UA_PLATFORM,
    SEC_CH_UA_MOBILE,
    SEC_CH_UA_FULL_VERSION_LIST,
    SEC_CH_UA_PLATFORM_VERSION,
    SEC_CH_UA_ARCH,
    SEC_CH_UA_BITNESS,
    SEC_CH_UA_MODEL,
    SEND_HIGH_ENTROPY_CLIENT_HINTS,
    ACCEPT_LANGUAGE,
    BROWSER_LOCALE_PROFILE,
    BROWSER_LOCALE_PROFILES,
    AUTO_BROWSER_LOCALE_FROM_IP,
    IP_GEO_TIMEOUT,
    IP_GEO_ENDPOINTS,
    COUNTRY_LOCALE_PROFILE_MAP,
    NAVIGATOR_LANGUAGE,
    NAVIGATOR_LANGUAGES,
    TIMEZONE_IANA,
    TIMEZONE_OFFSET_MINUTES,
    TIMEZONE_NAME,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    HARDWARE_CONCURRENCY,
    JS_HEAP_SIZE_LIMIT,
    DEVICE_MEMORY,
    NAVIGATOR_PROTO_SAMPLES,
    DOCUMENT_KEY_SAMPLES,
    WINDOW_KEY_SAMPLES,
    WINDOW_FEATURE_FLAGS,
    build_browser_environment,
    validate_browser_profile,
    BROWSER_PROFILE_POOL,
    pick_browser_profile,
    IMPERSONATE,
    REQUEST_TIMEOUT,
)

# ---------- OpenAI 协议 ----------
from config.openai_protocol import (
    OPENAI_CLIENT_ID,
    OPENAI_SCOPE,
    OPENAI_AUDIENCE,
    OPENAI_REDIRECT_URI,
    SENTINEL_SV,
    OPENAI_BUILD_ID,
)

# ---------- 代理池 ----------
from config.proxy import (
    PROXY_POOL,
    pick_proxy,
    PROXY,
)

# ---------- 注册默认信息 ----------
from config.register import (
    REGISTER_EMAIL,
    REGISTER_PASSWORD,
    REGISTER_NAME,
)

# ---------- 邮箱服务 ----------
from config.email import (
    USE_EMAIL_SERVICE,
    EMAIL_SOURCE,
    OUTLOOK_ACCOUNTS_FILE,
    OUTLOOK_API_BASE,
    OTP_POLL_INTERVAL,
    OTP_MAX_WAIT,
    OTP_SETTLE_SECONDS,
    EMAIL_DOMAIN,
    QQ_IMAP_SERVER,
    QQ_IMAP_PORT,
    QQ_EMAIL,
    QQ_IMAP_PASSWORD,
)

# ---------- 2FA ----------
from config.twofa import ENABLE_2FA


# ---------- 热加载支持 ----------
# WebUI 改配置后调 reload_all() 即可让所有运行时代码看到新值，无需重启进程。
# 前提：运行时代码读配置时用 `config.<子模块>.KEY` 形式（而不是 `from config.子模块 import KEY` 把值绑死）。
# 比如 `from config import codex; ... codex.SMS_COUNTRY`，reload 后 codex 模块对象原地更新，
# 引用 codex.SMS_COUNTRY 立即看到新值。
import importlib as _importlib

_RELOADABLE_SUBMODULES = (
    "config.browser",
    "config.openai_protocol",
    "config.proxy",
    "config.register",
    "config.email",
    "config.twofa",
    "config.roxybrowser",
    "config.cloakbrowser",
    "config.flow_trigger",
    "config.codex",
    "config.humanize",
)


def reload_all() -> list[str]:
    """
    热重载所有 config 子模块，返回成功 reload 的模块名列表。
    任何子模块 reload 失败（语法错等）会抛 ImportError，调用方自行处理。
    """
    import sys
    reloaded = []
    for name in _RELOADABLE_SUBMODULES:
        mod = sys.modules.get(name)
        if mod is None:
            mod = _importlib.import_module(name)
        else:
            _importlib.reload(mod)
        reloaded.append(name)
    # 同步刷新 config 包顶层的"被绑死"常量（兼容历史 `from config import X` 用法）
    # 注意：通过这些名字读到的是 reload 前的值，但子模块属性方式不受影响。
    _refresh_top_level_constants()
    return reloaded


def _refresh_top_level_constants() -> None:
    """把刚 reload 的子模块的常量重新拷一份到 config 包顶层。"""
    import config as _self
    from config import browser, openai_protocol, proxy as _proxy, register, email, twofa, roxybrowser, cloakbrowser, codex, humanize, flow_trigger
    # 简单粗暴：枚举一遍重要常量，覆盖到 _self
    for src in (browser, openai_protocol, _proxy, register, email, twofa, roxybrowser, cloakbrowser, codex, humanize, flow_trigger):
        for k in dir(src):
            if k.isupper() or k in ("pick_proxy", "pick_browser_profile", "build_browser_environment", "validate_browser_profile"):
                setattr(_self, k, getattr(src, k))


__all__ = [
    # browser
    "USER_AGENT", "CHROME_MAJOR", "CHROME_FULL_VERSION",
    "SEC_CH_UA", "SEC_CH_UA_PLATFORM", "SEC_CH_UA_MOBILE",
    "SEC_CH_UA_FULL_VERSION_LIST", "SEC_CH_UA_PLATFORM_VERSION",
    "SEC_CH_UA_ARCH", "SEC_CH_UA_BITNESS", "SEC_CH_UA_MODEL",
    "SEND_HIGH_ENTROPY_CLIENT_HINTS", "ACCEPT_LANGUAGE", "BROWSER_LOCALE_PROFILE", "BROWSER_LOCALE_PROFILES",
    "AUTO_BROWSER_LOCALE_FROM_IP", "IP_GEO_TIMEOUT", "IP_GEO_ENDPOINTS", "COUNTRY_LOCALE_PROFILE_MAP",
    "NAVIGATOR_LANGUAGE", "NAVIGATOR_LANGUAGES",
    "TIMEZONE_IANA", "TIMEZONE_OFFSET_MINUTES", "TIMEZONE_NAME", "SCREEN_WIDTH", "SCREEN_HEIGHT",
    "HARDWARE_CONCURRENCY", "JS_HEAP_SIZE_LIMIT", "DEVICE_MEMORY",
    "NAVIGATOR_PROTO_SAMPLES", "DOCUMENT_KEY_SAMPLES", "WINDOW_KEY_SAMPLES", "WINDOW_FEATURE_FLAGS",
    "build_browser_environment", "validate_browser_profile",
    "BROWSER_PROFILE_POOL", "pick_browser_profile",
    "IMPERSONATE", "REQUEST_TIMEOUT",
    # openai_protocol
    "OPENAI_CLIENT_ID", "OPENAI_SCOPE", "OPENAI_AUDIENCE", "OPENAI_REDIRECT_URI",
    "SENTINEL_SV", "OPENAI_BUILD_ID",
    # proxy
    "PROXY_POOL", "pick_proxy", "PROXY",
    # register
    "REGISTER_EMAIL", "REGISTER_PASSWORD", "REGISTER_NAME",
    # email
    "USE_EMAIL_SERVICE", "EMAIL_SOURCE",
    "OUTLOOK_ACCOUNTS_FILE", "OUTLOOK_API_BASE",
    "OTP_POLL_INTERVAL", "OTP_MAX_WAIT", "OTP_SETTLE_SECONDS",
    "EMAIL_DOMAIN", "QQ_IMAP_SERVER", "QQ_IMAP_PORT", "QQ_EMAIL", "QQ_IMAP_PASSWORD",
    # twofa
    "ENABLE_2FA",
]
