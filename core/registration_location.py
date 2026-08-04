from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

LOCATION_URL = "https://ipapi.co/json/"
LOCATION_TIMEOUT = 5.0
_SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


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
