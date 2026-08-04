from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import unquote, urlparse

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

LOCATION_URL = "https://ipapi.co/json/"
LOCATION_TIMEOUT = 5.0
_SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


def _country_code_from_proxy_username(proxy_url: str) -> str:
    try:
        username = unquote(urlparse(str(proxy_url or "").strip()).username or "")
    except ValueError:
        return ""
    match = re.search(r"(?:^|-)region-([A-Za-z]{2})(?:-|$)", username, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def infer_registration_country_code(proxy_url: str | None) -> str:
    """Infer configured proxy country when the live IP lookup has no result."""
    proxy = str(proxy_url or "").strip()
    direct = _country_code_from_proxy_username(proxy)
    if direct:
        return direct

    try:
        parsed = urlparse(proxy)
        from config import proxy as proxy_cfg

        is_local_bridge = (
            bool(getattr(proxy_cfg, "PROXY_CHAIN_ENABLED", False))
            and parsed.hostname == str(
                getattr(proxy_cfg, "PROXY_CHAIN_LISTEN_HOST", "127.0.0.1") or "127.0.0.1"
            )
            and parsed.port == int(getattr(proxy_cfg, "PROXY_CHAIN_LISTEN_PORT", 25001) or 25001)
            and unquote(parsed.username or "").startswith("sid-")
            and unquote(parsed.password or "") == "bridge"
        )
    except (TypeError, ValueError):
        return ""
    if not is_local_bridge:
        return ""
    return _country_code_from_proxy_username(
        str(getattr(proxy_cfg, "PROXY_CHAIN_UPSTREAM", "") or "")
    )


def _valid_proxy_url(proxy_url: str | None) -> bool:
    try:
        parsed = urlparse(str(proxy_url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in _SUPPORTED_PROXY_SCHEMES
        and bool(parsed.hostname)
        and port is not None
    )


def _default_transport(url: str, proxy_url: str, timeout: float) -> dict:
    response = curl_requests.get(
        url,
        headers={"Accept": "application/json"},
        proxy=proxy_url,
        timeout=timeout,
    )
    if not 200 <= int(response.status_code) < 300:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError("provider returned an error response")
    return payload


def lookup_registration_location(
    proxy_url: str | None,
    *,
    timeout: float = LOCATION_TIMEOUT,
    transport: Callable[[str, str, float], dict] | None = None,
) -> dict:
    proxy = str(proxy_url or "").strip()
    if not _valid_proxy_url(proxy):
        return {}

    request_json = transport or _default_transport
    try:
        payload = request_json(LOCATION_URL, proxy, float(timeout))
        return {
            "country_code": str(
                payload.get("country_code") or payload.get("country") or ""
            ).strip().upper(),
            "country": str(payload.get("country_name") or "").strip(),
            "region": str(
                payload.get("region") or payload.get("region_name") or ""
            ).strip(),
            "ip": str(payload.get("ip") or "").strip(),
        }
    except Exception as exc:
        logger.warning("注册出口位置查询失败：%s", type(exc).__name__)
        return {}
