# -*- coding: utf-8 -*-
"""内置代理平台适配器、代理生成和连通性测试。"""
from __future__ import annotations

import secrets
import string
from collections.abc import Mapping
from urllib.parse import quote, urlsplit



def _proxy_config_module():
    """延迟取得 config.proxy，避免直接导入本模块时形成循环导入。"""
    from config import proxy as proxy_config

    return proxy_config


COUNTRY_OPTIONS = (
    ("ID", "印度尼西亚"),
    ("US", "美国"),
    ("GB", "英国"),
    ("CA", "加拿大"),
    ("AU", "澳大利亚"),
    ("DE", "德国"),
    ("FR", "法国"),
    ("JP", "日本"),
    ("SG", "新加坡"),
    ("NL", "荷兰"),
    ("BR", "巴西"),
    ("KR", "韩国"),
    ("TW", "中国台湾"),
)

# 两个平台都支持的国家资源远大于旧版下拉框的 13 项。这里保留 provider
# 选择所需的 ISO-3166-1 两位码；实际可用性仍由供应商账户套餐决定。
# CliProxy/代理资源页与 IPRoyal Residential 国家接口均使用 ISO 两位码，
# 因而 UI 只发送这些非敏感的定位参数，不把账号密码放入目录。
_EXPANDED_COUNTRIES = (
    ("AF", "阿富汗"), ("AL", "阿尔巴尼亚"), ("DZ", "阿尔及利亚"), ("AR", "阿根廷"),
    ("AM", "亚美尼亚"), ("AT", "奥地利"), ("AZ", "阿塞拜疆"), ("BH", "巴林"),
    ("BD", "孟加拉国"), ("BY", "白俄罗斯"), ("BE", "比利时"), ("BZ", "伯利兹"),
    ("BO", "玻利维亚"), ("BA", "波黑"), ("BG", "保加利亚"), ("KH", "柬埔寨"),
    ("CL", "智利"), ("CN", "中国"), ("CO", "哥伦比亚"), ("CR", "哥斯达黎加"),
    ("HR", "克罗地亚"), ("CY", "塞浦路斯"), ("EE", "爱沙尼亚"), ("EG", "埃及"),
    ("FI", "芬兰"), ("GE", "格鲁吉亚"), ("GR", "希腊"), ("GT", "危地马拉"),
    ("HN", "洪都拉斯"), ("IS", "冰岛"), ("IE", "爱尔兰"), ("IL", "以色列"),
    ("IT", "意大利"), ("JO", "约旦"), ("KZ", "哈萨克斯坦"), ("KE", "肯尼亚"),
    ("KW", "科威特"), ("LV", "拉脱维亚"), ("LB", "黎巴嫩"), ("LT", "立陶宛"),
    ("LU", "卢森堡"), ("MT", "马耳他"), ("MU", "毛里求斯"), ("MD", "摩尔多瓦"),
    ("MC", "摩纳哥"), ("MN", "蒙古"), ("ME", "黑山"), ("MA", "摩洛哥"),
    ("NP", "尼泊尔"), ("NI", "尼加拉瓜"), ("NG", "尼日利亚"), ("MK", "北马其顿"),
    ("PA", "巴拿马"), ("PY", "巴拉圭"), ("PE", "秘鲁"), ("PK", "巴基斯坦"),
    ("PH", "菲律宾"), ("PT", "葡萄牙"), ("QA", "卡塔尔"), ("RO", "罗马尼亚"),
    ("RU", "俄罗斯"), ("SA", "沙特阿拉伯"), ("RS", "塞尔维亚"), ("SK", "斯洛伐克"),
    ("SI", "斯洛文尼亚"), ("ZA", "南非"), ("ES", "西班牙"), ("LK", "斯里兰卡"),
    ("TT", "特立尼达和多巴哥"), ("TN", "突尼斯"), ("UA", "乌克兰"), ("AE", "阿联酋"),
    ("UY", "乌拉圭"), ("UZ", "乌兹别克斯坦"), ("VE", "委内瑞拉"), ("VN", "越南"),
    ("ZM", "赞比亚"), ("ZW", "津巴布韦"), ("US", "美国"), ("GB", "英国"),
    ("CA", "加拿大"), ("AU", "澳大利亚"), ("DE", "德国"), ("FR", "法国"),
    ("JP", "日本"), ("SG", "新加坡"), ("NL", "荷兰"), ("BR", "巴西"),
    ("KR", "韩国"), ("TW", "中国台湾"), ("ID", "印度尼西亚"),
    ("IN", "印度"), ("MY", "马来西亚"), ("TH", "泰国"), ("HK", "中国香港"),
    ("MO", "中国澳门"), ("NZ", "新西兰"), ("CH", "瑞士"), ("SE", "瑞典"),
    ("NO", "挪威"), ("DK", "丹麦"), ("FI", "芬兰"), ("PL", "波兰"),
    ("CZ", "捷克"), ("HU", "匈牙利"), ("TR", "土耳其"), ("IE", "爱尔兰"),
    ("IS", "冰岛"), ("ES", "西班牙"), ("IT", "意大利"), ("PT", "葡萄牙"),
    ("AT", "奥地利"), ("BE", "比利时"), ("IL", "以色列"), ("AE", "阿联酋"),
    ("SA", "沙特阿拉伯"), ("QA", "卡塔尔"), ("PK", "巴基斯坦"), ("BD", "孟加拉国"),
    ("LK", "斯里兰卡"), ("MM", "缅甸"), ("KH", "柬埔寨"), ("LA", "老挝"),
    ("MN", "蒙古"), ("KZ", "哈萨克斯坦"), ("UZ", "乌兹别克斯坦"), ("GH", "加纳"),
    ("TZ", "坦桑尼亚"), ("UG", "乌干达"), ("MA", "摩洛哥"), ("DZ", "阿尔及利亚"),
    ("ET", "埃塞俄比亚"), ("CM", "喀麦隆"), ("CI", "科特迪瓦"), ("SN", "塞内加尔"),
    ("EC", "厄瓜多尔"), ("DO", "多米尼加"), ("JM", "牙买加"), ("PR", "波多黎各"),
    ("BS", "巴哈马"), ("BB", "巴巴多斯"), ("GU", "关岛"), ("MP", "北马里亚纳群岛"),
)

# 供应商目录单独命名，便于后续按供应商接口刷新而不改变前端协议。
# 目前两者的静态候选集合取各自公开资源的交集，避免把未在资源页出现
# 的地区误报成“可用”；账户套餐/库存不足时接口仍会返回错误。
COUNTRY_OPTIONS = tuple(dict.fromkeys(COUNTRY_OPTIONS + _EXPANDED_COUNTRIES))
PROVIDER_COUNTRY_OPTIONS = {
    "cliproxy_traffic": tuple(dict.fromkeys(COUNTRY_OPTIONS + _EXPANDED_COUNTRIES)),
    "iproyal_traffic": tuple(dict.fromkeys(COUNTRY_OPTIONS + _EXPANDED_COUNTRIES)),
}

PROXY_PROVIDER_LABELS = {
    "cliproxy_traffic": "CliProxy",
    "iproyal_traffic": "IPRoyal",
}

_COUNTRY_ALIASES = {
    "美国": "US", "UNITED STATES": "US", "USA": "US",
    "印度尼西亚": "ID", "印尼": "ID", "INDONESIA": "ID",
    "菲律宾": "PH", "PHILIPPINES": "PH",
    "新加坡": "SG", "SINGAPORE": "SG",
    "日本": "JP", "JAPAN": "JP",
    "英国": "GB", "UNITED KINGDOM": "GB", "UK": "GB",
    "加拿大": "CA", "CANADA": "CA",
    "澳大利亚": "AU", "AUSTRALIA": "AU",
    "德国": "DE", "GERMANY": "DE",
    "法国": "FR", "FRANCE": "FR",
    "荷兰": "NL", "NETHERLANDS": "NL",
    "巴西": "BR", "BRAZIL": "BR",
    "韩国": "KR", "SOUTH KOREA": "KR", "KOREA": "KR",
    "台湾": "TW", "中国台湾": "TW", "TAIWAN": "TW",
}

_BUILTIN_PROVIDERS = set(PROXY_PROVIDER_LABELS)


def _config_value(settings: Mapping[str, object] | None, key: str, default: object = "") -> object:
    if settings is not None and key in settings:
        return settings.get(key)
    return getattr(_proxy_config_module(), key, default)


def _configured(settings: Mapping[str, object] | None, key: str) -> bool:
    return bool(str(_config_value(settings, key, "") or "").strip())


def normalize_country(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("代理国家不能为空")
    upper = text.upper()
    country = _COUNTRY_ALIASES.get(upper, _COUNTRY_ALIASES.get(text, upper))
    if len(country) != 2 or not country.isalpha():
        raise ValueError(f"代理国家必须是 ISO 两位代码: {text}")
    return country.upper()


def _sid() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def _positive_int(settings: Mapping[str, object] | None, key: str, label: str, *, minimum: int = 1, maximum: int = 65535) -> int:
    try:
        value = int(_config_value(settings, key, 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}必须是 {minimum}-{maximum}")
    return value


def _sticky_minutes(settings: Mapping[str, object] | None, key: str) -> int:
    return _positive_int(settings, key, "代理 sticky 会话时长", minimum=1, maximum=120)


def _cliproxy_config(settings: Mapping[str, object] | None = None) -> tuple[str, str, str, int, int]:
    username = str(_config_value(settings, "CLIPROXY_PROXY_USERNAME", "") or "").strip()
    password = str(_config_value(settings, "CLIPROXY_PROXY_PASSWORD", "") or "").strip()
    host = str(_config_value(settings, "CLIPROXY_FORWARD_HOST", "") or "").strip()
    if not username:
        raise ValueError("CliProxy 账号未配置")
    if not password:
        raise ValueError("CliProxy 密码未配置")
    if not host:
        raise ValueError("CliProxy 转发主机未配置")
    port = _positive_int(settings, "CLIPROXY_FORWARD_PORT", "CliProxy 转发端口")
    minutes = _sticky_minutes(settings, "CLIPROXY_STICKY_MINUTES")
    return username, password, host, port, minutes


def _iproyal_config(settings: Mapping[str, object] | None = None) -> tuple[str, str, str, int, int]:
    username = str(_config_value(settings, "IPROYAL_PROXY_USERNAME", "") or "").strip()
    password = str(_config_value(settings, "IPROYAL_PROXY_PASSWORD", "") or "").strip()
    host = str(_config_value(settings, "IPROYAL_FORWARD_HOST", "geo.iproyal.com") or "").strip()
    if not username:
        raise ValueError("IPRoyal 账号未配置")
    if not password:
        raise ValueError("IPRoyal 密码未配置")
    if not host:
        raise ValueError("IPRoyal 转发主机未配置")
    port = _positive_int(settings, "IPROYAL_FORWARD_PORT", "IPRoyal 转发端口")
    minutes = _sticky_minutes(settings, "IPROYAL_STICKY_MINUTES")
    return username, password, host, port, minutes


def _default_country(provider: str, settings: Mapping[str, object] | None = None) -> str:
    key = "CLIPROXY_PROXY_COUNTRY" if provider == "cliproxy_traffic" else "IPROYAL_PROXY_COUNTRY"
    return str(_config_value(settings, key, "") or "").strip()


def validate_provider(
    provider: str | None,
    country: str | None = None,
    *,
    settings: Mapping[str, object] | None = None,
) -> str:
    name = str(provider or _config_value(settings, "PROXY_PROVIDER", "manual") or "manual").strip().lower()
    if name not in {"manual", *_BUILTIN_PROVIDERS}:
        raise ValueError(f"不支持的内置代理平台: {name}")
    if name == "cliproxy_traffic":
        _cliproxy_config(settings)
    elif name == "iproyal_traffic":
        _iproyal_config(settings)
    if name in _BUILTIN_PROVIDERS:
        normalize_country(country or _default_country(name, settings))
    return name


def build_proxy(
    provider: str | None = None,
    country: str | None = None,
    *,
    job_id: int | None = None,
    attempt: int = 0,
    settings: Mapping[str, object] | None = None,
) -> str:
    """生成一次任务专用代理 URL；账号密码只进入 URL，不进入返回的展示字段。"""
    name = validate_provider(provider, country, settings=settings)
    if name == "manual":
        return _proxy_config_module()._pick_manual_proxy()

    country_code = normalize_country(country or _default_country(name, settings))
    session_id = _sid()
    if name == "cliproxy_traffic":
        username, password, host, port, minutes = _cliproxy_config(settings)
        session_user = f"{username}-region-{country_code}-sid-{session_id}-t-{minutes}"
        return f"socks5h://{quote(session_user, safe='')}:{quote(password, safe='')}@{host}:{port}"

    username, password, host, port, minutes = _iproyal_config(settings)
    # IPRoyal Residential 使用 password 后缀承载国家和 sticky session 参数。
    session_password = f"{password}_country-{country_code.lower()}_session-{session_id}_lifetime-{minutes}m"
    return f"http://{quote(username, safe='')}:{quote(session_password, safe='')}@{host}:{port}"


def provider_catalog() -> list[dict[str, object]]:
    """返回任务中心可用平台目录；结果不含账号、密码或生成的代理 URL。"""
    return [
        {
            "value": "cliproxy_traffic",
            "label": PROXY_PROVIDER_LABELS["cliproxy_traffic"],
            "configured": _configured(None, "CLIPROXY_PROXY_USERNAME") and _configured(None, "CLIPROXY_PROXY_PASSWORD"),
            "host": str(_config_value(None, "CLIPROXY_FORWARD_HOST", "") or ""),
            "port": int(_config_value(None, "CLIPROXY_FORWARD_PORT", 0) or 0),
            "country": str(_config_value(None, "CLIPROXY_PROXY_COUNTRY", "ID") or "ID").upper(),
            "country_source": "provider_resource_catalog",
            "countries": country_catalog_for_provider("cliproxy_traffic"),
        },
        {
            "value": "iproyal_traffic",
            "label": PROXY_PROVIDER_LABELS["iproyal_traffic"],
            "configured": _configured(None, "IPROYAL_PROXY_USERNAME") and _configured(None, "IPROYAL_PROXY_PASSWORD"),
            "host": str(_config_value(None, "IPROYAL_FORWARD_HOST", "geo.iproyal.com") or "geo.iproyal.com"),
            "port": int(_config_value(None, "IPROYAL_FORWARD_PORT", 12321) or 12321),
            "country": str(_config_value(None, "IPROYAL_PROXY_COUNTRY", "ID") or "ID").upper(),
            "country_source": "provider_resource_catalog",
            "countries": country_catalog_for_provider("iproyal_traffic"),
        },
    ]


def country_catalog() -> list[dict[str, str]]:
    return country_catalog_for_provider(None)


def country_catalog_for_provider(provider: str | None) -> list[dict[str, str]]:
    """返回指定供应商的实际 ISO 国家候选；未知供应商使用两者交集。"""
    name = str(provider or "").strip().lower()
    options = PROVIDER_COUNTRY_OPTIONS.get(name)
    if options is None:
        options = tuple(dict.fromkeys(COUNTRY_OPTIONS + _EXPANDED_COUNTRIES))
    return [{"value": code, "label": label} for code, label in options]


def settings_from_values(provider: str, values: Mapping[str, object]) -> dict[str, object]:
    """把前端的通用字段映射为 provider-specific 配置键。"""
    name = str(provider or "").strip().lower()
    if name == "cliproxy_traffic":
        prefix = "CLIPROXY"
    elif name == "iproyal_traffic":
        prefix = "IPROYAL"
    else:
        raise ValueError(f"不支持的内置代理平台: {name}")

    defaults = {
        "cliproxy_traffic": {
            "host": getattr(_proxy_config_module(), "CLIPROXY_FORWARD_HOST", ""),
            "port": getattr(_proxy_config_module(), "CLIPROXY_FORWARD_PORT", 3000),
            "sticky_minutes": getattr(_proxy_config_module(), "CLIPROXY_STICKY_MINUTES", 60),
            "country": getattr(_proxy_config_module(), "CLIPROXY_PROXY_COUNTRY", "ID"),
        },
        "iproyal_traffic": {
            "host": getattr(_proxy_config_module(), "IPROYAL_FORWARD_HOST", "geo.iproyal.com"),
            "port": getattr(_proxy_config_module(), "IPROYAL_FORWARD_PORT", 12321),
            "sticky_minutes": getattr(_proxy_config_module(), "IPROYAL_STICKY_MINUTES", 60),
            "country": getattr(_proxy_config_module(), "IPROYAL_PROXY_COUNTRY", "ID"),
        },
    }[name]

    def value_or_default(key: str) -> object:
        value = values.get(key)
        return defaults[key] if value is None or str(value).strip() == "" else value

    def credential_or_saved(key: str) -> str:
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        return str(_config_value(None, f"{prefix}_PROXY_{key.upper()}", "") or "").strip()

    return {
        f"{prefix}_PROXY_USERNAME": credential_or_saved("username"),
        f"{prefix}_PROXY_PASSWORD": credential_or_saved("password"),
        f"{prefix}_FORWARD_HOST": str(value_or_default("host") or "").strip(),
        f"{prefix}_FORWARD_PORT": value_or_default("port"),
        f"{prefix}_STICKY_MINUTES": value_or_default("sticky_minutes"),
        f"{prefix}_PROXY_COUNTRY": str(value_or_default("country") or "").strip().upper(),
    }


def test_proxy_connection(proxy_url: str, *, timeout: float = 15.0, test_url: str = "https://api.ipify.org?format=json") -> dict[str, int | str | bool]:
    """通过代理请求一个轻量 HTTPS 地址，只返回状态摘要，不返回出口 IP。"""
    from curl_cffi.requests import Session

    session = Session(impersonate="chrome")
    try:
        response = session.get(
            test_url,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=float(timeout),
        )
        status_code = int(response.status_code)
        if not 200 <= status_code < 400:
            raise RuntimeError(f"代理测试返回 HTTP {status_code}")
        return {"ok": True, "status_code": status_code, "test_url": test_url}
    finally:
        try:
            session.close()
        except Exception:
            pass


def mask_proxy_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return "已配置代理"
    port = ""
    try:
        if parsed.port:
            port = f":{parsed.port}"
    except ValueError:
        pass
    username = parsed.username or ""
    if parsed.password is not None:
        return f"{parsed.scheme}://{username}:***@{parsed.hostname}{port}"
    return f"{parsed.scheme}://{parsed.hostname}{port}"
