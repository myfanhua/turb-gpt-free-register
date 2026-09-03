# -*- coding: utf-8 -*-
"""
内置代理平台配置

每次任务根据所选平台、国家和会话参数生成独立代理；旧版手动 URL 列表仅保留兼容读取。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
import random
import re
from urllib.parse import quote, unquote, urlsplit


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

PROXY_DEFAULT_SCHEME = "http"
PROXY_PROVIDER = "cliproxy_traffic"
CLIPROXY_PROXY_USERNAME = ""
CLIPROXY_PROXY_PASSWORD = ""
CLIPROXY_FORWARD_HOST = "us.cliproxy.io"
CLIPROXY_FORWARD_PORT = 3000
CLIPROXY_STICKY_MINUTES = 60
# CliProxy 内置代理默认出口国家；可在 WebUI 中改成 ISO 两位码或国家名。
CLIPROXY_PROXY_COUNTRY = "ID"

# IPRoyal Residential 内置代理默认连接参数；账号/密码由 .env 保存。
# IPRoyal 的国家和 sticky session 通过密码后缀传给 geo.iproyal.com。
IPROYAL_PROXY_USERNAME = ""
IPROYAL_PROXY_PASSWORD = ""
IPROYAL_FORWARD_HOST = "geo.iproyal.com"
IPROYAL_FORWARD_PORT = 12321
IPROYAL_STICKY_MINUTES = 60
IPROYAL_PROXY_COUNTRY = "ID"

# 1024 代理平台兼容字段；默认关闭，只有选择 1024proxy_traffic 时使用。
PROXY_1024_USERNAME = ""
PROXY_1024_PASSWORD = ""
PROXY_1024_FORWARD_HOST = ""
PROXY_1024_FORWARD_PORT = 0

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
#   auto   = 优先使用 PLAN_CHECK_PROXY 或当前内置代理；不可用时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或当前内置代理，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式使用当前内置代理。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


_PROXY_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_PROXY_DEFAULT_SCHEMES = ("http", "socks5")


def _normalize_default_scheme(value: str | None) -> str:
    scheme = str(value or "http").strip().lower()
    return scheme if scheme in _PROXY_DEFAULT_SCHEMES else "http"


def normalize_proxy_url(proxy_url: str, default_scheme: str | None = None) -> str:
    """把代理行转换为 URL，兼容 host:port:user:pass 格式。"""
    text = str(proxy_url or "").strip()
    if not text:
        return ""
    scheme_match = _PROXY_SCHEME_RE.match(text)
    scheme = scheme_match.group(0)[:-3].lower() if scheme_match else _normalize_default_scheme(default_scheme or PROXY_DEFAULT_SCHEME)
    body = text[scheme_match.end():] if scheme_match else text
    parts = body.split(":")
    if "@" not in body and len(parts) >= 4 and parts[1].isdigit():
        host, port, username = parts[:3]
        password = ":".join(parts[3:])
        if host and username and password:
            body = f"{quote(unquote(username), safe='')}:{quote(unquote(password), safe='')}@{host}:{port}"
    normalized = text if scheme_match and body == text[scheme_match.end():] else f"{scheme}://{body}"
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"}:
        if not parsed.hostname:
            raise ValueError(f"代理格式缺少 host: {normalized}")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"代理端口无效: {normalized}") from exc
        if port is None or not 1 <= int(port) <= 65535:
            raise ValueError(f"代理端口无效: {normalized}")
    return normalized


def normalize_proxy_pool(proxy_pool, default_scheme: str | None = None) -> list[str]:
    values = proxy_pool.splitlines() if isinstance(proxy_pool, str) else (proxy_pool or [])
    return [normalized for value in values if (normalized := normalize_proxy_url(value, default_scheme))]


def _pick_manual_proxy() -> str:
    """从手动代理池中随机抽取一个带协议的代理 URL。"""
    pool = normalize_proxy_pool(PROXY_POOL, PROXY_DEFAULT_SCHEME)
    return random.choice(pool) if pool else ""


def pick_proxy() -> str:
    """按 PROXY_PROVIDER 选择内置代理或从手动代理池随机抽取。"""
    provider = str(PROXY_PROVIDER or "manual").strip().lower()
    if provider in {"cliproxy_traffic", "iproyal_traffic"}:
        # 延迟导入，避免 config.proxy 与 provider 模块在启动时循环导入。
        from core.proxy_provider import build_proxy

        country_key = (
            "CLIPROXY_PROXY_COUNTRY"
            if provider == "cliproxy_traffic"
            else "IPROYAL_PROXY_COUNTRY"
        )
        try:
            return build_proxy(provider, globals().get(country_key, ""))
        except ValueError:
            # 未填写平台账号密码时允许模块正常启动；提交任务时会返回明确的配置错误。
            return ""
    return _pick_manual_proxy()


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_DEFAULT_SCHEME': 'str',
    'PROXY_PROVIDER': 'str',
    'CLIPROXY_PROXY_USERNAME': 'str',
    'CLIPROXY_PROXY_PASSWORD': 'str',
    'CLIPROXY_FORWARD_HOST': 'str',
    'CLIPROXY_FORWARD_PORT': 'int',
    'CLIPROXY_STICKY_MINUTES': 'int',
    'CLIPROXY_PROXY_COUNTRY': 'str',
    'IPROYAL_PROXY_USERNAME': 'str',
    'IPROYAL_PROXY_PASSWORD': 'str',
    'IPROYAL_FORWARD_HOST': 'str',
    'IPROYAL_FORWARD_PORT': 'int',
    'IPROYAL_STICKY_MINUTES': 'int',
    'IPROYAL_PROXY_COUNTRY': 'str',
    'PROXY_1024_USERNAME': 'str',
    'PROXY_1024_PASSWORD': 'str',
    'PROXY_1024_FORWARD_HOST': 'str',
    'PROXY_1024_FORWARD_PORT': 'int',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY_DEFAULT_SCHEME = _normalize_default_scheme(PROXY_DEFAULT_SCHEME)
PROXY = pick_proxy()
