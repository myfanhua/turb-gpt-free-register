# -*- coding: utf-8 -*-
"""只读试用支付方式探测。

该模块走真实 ChatGPT checkout 预览与 Stripe Elements 初始化接口，
停在拿到 payment method specs 的阶段，绝不调用 payment_pages/*/confirm。
返回值只包含方式状态摘要，不包含 checkout session、client secret、token 或原始响应。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

from core.chatgpt_plan import _common_headers, normalize_token, token_claims
from core.session import BrowserSession

CHATGPT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION = "2025-03-31.basil"
DEFAULT_CAMPAIGN_ID = "plus-1-month-free"

# 这些是前端公开使用的 publishable key，仅用于 Stripe 初始化；可由配置覆盖。
DEFAULT_STRIPE_PUBLISHABLE_KEYS = (
    "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
)

TRIAL_PAYMENT_METHODS = (
    {"id": "momo", "label": "Momo"},
    {"id": "paypal", "label": "PayPal"},
    {"id": "gopay", "label": "GoPay"},
)

_CURRENCY_BY_COUNTRY = {
    "ID": "idr", "VN": "vnd", "IN": "inr", "JP": "jpy", "KR": "krw",
    "BR": "brl", "GB": "gbp", "AU": "aud", "CA": "cad", "SG": "sgd",
    "MY": "myr", "TH": "thb", "PH": "php", "TW": "twd", "HK": "hkd",
    "MX": "mxn", "NZ": "nzd", "CH": "chf", "SE": "sek", "NO": "nok",
    "DK": "dkk", "PL": "pln", "CZ": "czk", "HU": "huf", "TR": "try",
}
_TIMEZONE_BY_COUNTRY = {
    "US": "America/Chicago", "CA": "America/Toronto", "GB": "Europe/London",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "NL": "Europe/Amsterdam",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "SG": "Asia/Singapore",
    "ID": "Asia/Jakarta", "IN": "Asia/Kolkata", "AU": "Australia/Sydney",
    "BR": "America/Sao_Paulo", "MX": "America/Mexico_City", "PH": "Asia/Manila",
    "TH": "Asia/Bangkok", "MY": "Asia/Kuala_Lumpur", "TW": "Asia/Taipei",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalise_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 80 or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", text):
        return ""
    return text


def parse_payment_method_types(payload: Any) -> list[str]:
    """从 Stripe init/elements 响应提取去重后的方式类型。"""
    if not isinstance(payload, dict):
        return []
    values: list[Any] = []
    direct = payload.get("payment_method_types")
    if isinstance(direct, (list, tuple)):
        values.extend(direct)
    specs = payload.get("payment_method_specs")
    if isinstance(specs, (list, tuple)):
        values.extend(item.get("type") for item in specs if isinstance(item, dict))
    # 某些版本将 specs 放在 payment_method_configuration 下面。
    nested = payload.get("payment_method_configuration")
    if isinstance(nested, dict):
        nested_types = nested.get("payment_method_types")
        if isinstance(nested_types, (list, tuple)):
            values.extend(nested_types)
        nested_specs = nested.get("payment_method_specs")
        if isinstance(nested_specs, (list, tuple)):
            values.extend(item.get("type") for item in nested_specs if isinstance(item, dict))
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("type")
        method = _normalise_type(value)
        if method and method not in result:
            result.append(method)
    return result


def _safe_status(response: Any) -> int | None:
    try:
        value = int(getattr(response, "status_code", 0) or 0)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _json_response(response: Any) -> dict[str, Any] | None:
    try:
        data = response.json()
    except Exception:
        try:
            raw = str(getattr(response, "text", "") or "")
            data = json.loads(raw) if raw.strip().startswith(("{", "[")) else None
        except Exception:
            data = None
    return data if isinstance(data, dict) else None


def _safe_response_reason(data: dict[str, Any] | None) -> str:
    """提取错误码/短消息，丢弃 id、secret 和原始响应。"""
    data = data or {}
    candidates: list[Any] = [data.get("code"), data.get("error_code"), data.get("message")]
    for key in ("error", "detail"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.extend((nested.get("code"), nested.get("error_code"), nested.get("message")))
        elif isinstance(nested, str):
            candidates.append(nested)
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        text = re.sub(
            r"(?:cs_(?:live|test)_[A-Za-z0-9]+|oaics_[A-Za-z0-9_-]+|cpmt_[A-Za-z0-9_-]+|pk_(?:live|test)_[A-Za-z0-9]+|client_secret[=:][^, }]+)",
            "[redacted]",
            text,
            flags=re.I,
        )
        return text[:120]
    return ""


def _find_checkout_identifier(data: Any, pattern: str) -> str:
    """在 checkout 响应的 JSON/URL 字符串中找会话标识，调用方不向外返回。"""
    matcher = re.compile(pattern)

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, str):
            match = matcher.search(value)
            if match:
                return match.group(0)
        return ""

    return walk(data)


def _checkout_session_id(data: dict[str, Any] | None) -> str:
    return _find_checkout_identifier(data or {}, r"cs_(?:live|test)_[A-Za-z0-9]+")


def _custom_checkout_session_id(data: dict[str, Any] | None) -> str:
    return _find_checkout_identifier(data or {}, r"oaics_[A-Za-z0-9_-]+")


def _custom_payment_methods(payload: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """读取 OAICS Checkout 的自定义支付方式列表；None 表示响应结构未知。"""
    if not isinstance(payload, dict):
        return None
    for key in ("custom_payment_methods", "custom_methods", "payment_methods", "methods"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("checkout_session", "checkout", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _custom_payment_methods(nested)
            if found is not None:
                return found
    return None


_CUSTOM_METHOD_MARKERS = {
    "momo": ("momo", "mo-mo"),
    "paypal": ("paypal", "pay pal"),
    "gopay": ("gopay", "go_pay", "go-pay"),
}


def _custom_method_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(value or "").lower()


def _custom_method_results(methods: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    available: list[str] = []
    rendered = [_custom_method_text(item) for item in methods]
    for method in TRIAL_PAYMENT_METHODS:
        method_id = method["id"]
        markers = _CUSTOM_METHOD_MARKERS[method_id]
        supported = any(any(marker in text for marker in markers) for text in rendered)
        status = "supported" if supported else "unsupported"
        detail = "OpenAI 自定义 Checkout 返回该方式" if supported else "OpenAI 自定义 Checkout 未返回该方式"
        results.append(_result_method(method_id, method["label"], status, detail))
        if supported:
            available.append(method_id)
    return results, available


def _custom_processor_entity(data: dict[str, Any] | None, country: str) -> str:
    candidate = str((data or {}).get("processor_entity") or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_-]{1,40}", candidate):
        return candidate
    return "openai_llc" if _country_code(country) == "US" else "openai_ie"


def _stripe_keys(values: list[str] | tuple[str, ...] | None = None) -> list[str]:
    if values is not None:
        keys = [str(value or "").strip() for value in values]
    else:
        configured = str(os.getenv("TRIAL_PAYMENT_STRIPE_PUBLISHABLE_KEYS", "") or "")
        keys = [item.strip() for item in configured.split(",")] if configured else list(DEFAULT_STRIPE_PUBLISHABLE_KEYS)
    return [key for key in keys if key.startswith("pk_") and len(key) >= 20]


def _country_code(value: str | None) -> str:
    text = str(value or "US").strip().upper()
    return text if re.fullmatch(r"[A-Z]{2}", text) else "US"


def _stripe_profile(country: str) -> tuple[str, str, str]:
    country = _country_code(country)
    currency = _CURRENCY_BY_COUNTRY.get(country, "usd")
    locale = "en-US"
    if country == "JP":
        locale = "ja-JP"
    elif country in {"ID", "MY", "SG", "PH"}:
        locale = "en-" + country
    elif country == "BR":
        locale = "pt-BR"
    elif country == "KR":
        locale = "ko-KR"
    return currency, locale, _TIMEZONE_BY_COUNTRY.get(country, "UTC")


def _request_headers(env: Any, token: str, *, target: str) -> dict[str, str]:
    try:
        headers = dict(_common_headers(env, token))
    except Exception:
        headers = {}
    headers.update({
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {normalize_token(token)}",
        "content-type": "application/json",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "x-openai-target-path": target,
        "x-openai-target-route": target,
    })
    return headers


def _stripe_headers(env: Any) -> dict[str, str]:
    headers: dict[str, str] = {"accept": "application/json", "content-type": "application/x-www-form-urlencoded"}
    profile = getattr(env, "browser_profile", {}) or {}
    user_agent = profile.get("user_agent")
    if user_agent:
        headers["user-agent"] = str(user_agent)
    return headers


def _session_from_factory(session_factory: Callable[..., Any] | None, proxy: str | None, seed: str) -> Any:
    if session_factory is None:
        return BrowserSession(proxy=proxy, detect_exit_geo=False, fingerprint_seed=seed)
    try:
        return session_factory(proxy=proxy, detect_exit_geo=False, fingerprint_seed=seed)
    except TypeError:
        try:
            return session_factory(proxy)
        except TypeError:
            return session_factory()


def _http_client(env: Any) -> Any:
    return getattr(env, "session", env)


def _result_method(method_id: str, label: str, status: str, detail: str | None = None) -> dict[str, str]:
    item = {"id": method_id, "label": label, "status": status}
    if detail:
        item["detail"] = str(detail)[:160]
    return item


def probe_trial_payment_methods(
    access_token: str,
    *,
    country: str = "US",
    proxy: str | None = None,
    campaign_id: str = DEFAULT_CAMPAIGN_ID,
    timeout: float = 30.0,
    session_factory: Callable[..., Any] | None = None,
    stripe_publishable_keys: list[str] | tuple[str, ...] | None = None,
    _allow_direct_fallback: bool = True,
) -> dict[str, Any]:
    """创建一次 checkout 预览并逐项读取 Momo/PayPal/GoPay 支持情况。"""
    token = normalize_token(access_token)
    checked_country = _country_code(country)
    methods = list(TRIAL_PAYMENT_METHODS)
    output: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "checked_at": now_iso(),
        "country": checked_country,
        "campaign_id": str(campaign_id or DEFAULT_CAMPAIGN_ID)[:120],
        "checkout_session_created": False,
        "available_method_types": [],
        "methods": [_result_method(item["id"], item["label"], "unknown", "尚未完成探测") for item in methods],
        "error": None,
    }
    if not token:
        output["error"] = "账号缺少 access_token"
        return output

    env = None
    try:
        claims = token_claims(token)
        seed = f"trial-payment:{claims.get('email') or claims.get('account_id') or token[:24]}"
        env = _session_from_factory(session_factory, proxy, seed)
        client = _http_client(env)
        currency, locale, timezone = _stripe_profile(checked_country)
        checkout_body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checked_country, "currency": currency.upper()},
            "checkout_ui_mode": "custom",
            "check_card_proxy": True,
            "promo_campaign": {
                "promo_campaign_id": str(campaign_id or DEFAULT_CAMPAIGN_ID),
                "is_coupon_from_query_param": False,
            },
        }
        response = client.post(
            CHATGPT_CHECKOUT_URL,
            headers=_request_headers(env, token, target="/backend-api/payments/checkout"),
            json=checkout_body,
            timeout=float(timeout),
            allow_redirects=False,
        )
        status_code = _safe_status(response)
        response_data = _json_response(response)
        if status_code is None or not 200 <= status_code < 300:
            reason = _safe_response_reason(response_data)
            output["error"] = f"ChatGPT checkout HTTP {status_code or 'network'}" + (f": {reason}" if reason else "")
            active_proxy = str(proxy or getattr(env, "proxy", "") or "").strip()
            reason_lower = reason.lower()
            if _allow_direct_fallback and active_proxy and (
                status_code in {403, 429} or "unusual activity" in reason_lower or "proxy" in reason_lower
            ):
                # 代理出口被风控时，按套餐查询的 auto 策略只回退一次真实直连；
                # 仍然只做 checkout 预览，不会重复确认或扣款。
                direct = probe_trial_payment_methods(
                    token,
                    country=checked_country,
                    proxy="",
                    campaign_id=campaign_id,
                    timeout=timeout,
                    session_factory=session_factory,
                    stripe_publishable_keys=stripe_publishable_keys,
                    _allow_direct_fallback=False,
                )
                direct["proxy_fallback"] = "direct"
                direct["proxy_fallback_reason"] = "代理出口触发风控，已重试真实直连"
                return direct
            return output
        session_id = _checkout_session_id(response_data)
        custom_session_id = _custom_checkout_session_id(response_data)
        if not session_id and not custom_session_id:
            output["error"] = "ChatGPT checkout 未返回有效会话"
            return output
        output["checkout_session_created"] = True

        # 新版 OAICS Checkout 不走 Stripe payment_pages，而是把可用方式
        # 放在 OpenAI 自定义 Checkout 的只读状态接口中；同样停在方式读取，
        # 不调用 checkout/confirm 或 custom_payment_method/start。
        if custom_session_id:
            processor = _custom_processor_entity(response_data, checked_country)
            custom_response = client.get(
                f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{custom_session_id}",
                headers=_request_headers(
                    env,
                    token,
                    target=f"/backend-api/payments/checkout/{processor}/{custom_session_id}",
                ),
                timeout=float(timeout),
            )
            custom_status = _safe_status(custom_response)
            custom_payload = _json_response(custom_response)
            if custom_status != 200:
                reason = _safe_response_reason(custom_payload)
                output["error"] = f"OpenAI custom checkout HTTP {custom_status or 'network'}" + (f": {reason}" if reason else "")
                return output
            custom_methods = _custom_payment_methods(custom_payload)
            if custom_methods is None:
                output["error"] = "OpenAI custom checkout 未返回支付方式列表"
                return output
            if not custom_methods:
                output["error"] = "OpenAI custom checkout 支付方式仍未同步"
                return output
            result_methods, available = _custom_method_results(custom_methods)
            output["methods"] = result_methods
            output["available_method_types"] = available
            output["ok"] = True
            output["status"] = "success"
            return output

        keys = _stripe_keys(stripe_publishable_keys)
        if not keys:
            output["error"] = "未配置 Stripe publishable key"
            return output

        init_payload: dict[str, Any] | None = None
        init_status: int | None = None
        active_key = keys[0]
        stripe_js_id = str(uuid.uuid4())
        customer_session_secret = str((response_data or {}).get("customer_session_client_secret") or "").strip()
        stripe_headers = _stripe_headers(env)
        for key in keys:
            init_data = {
                "browser_locale": locale,
                "browser_timezone": timezone,
                "elements_session_client[elements_init_source]": "custom_checkout",
                "elements_session_client[referrer_host]": "chatgpt.com",
                "elements_session_client[stripe_js_id]": stripe_js_id,
                "elements_session_client[locale]": locale,
                "elements_session_client[is_aggregation_expected]": "false",
                "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
                "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
                "key": key,
                "_stripe_version": STRIPE_VERSION,
            }
            init_response = client.post(
                f"{STRIPE_API}/v1/payment_pages/{session_id}/init",
                data=init_data,
                headers=stripe_headers,
                timeout=float(timeout),
            )
            init_status = _safe_status(init_response)
            if init_status == 200:
                init_payload = _json_response(init_response) or {}
                active_key = key
                break
        if init_payload is None:
            output["error"] = f"Stripe checkout init HTTP {init_status or 'network'}"
            return output

        available = parse_payment_method_types(init_payload)
        output["available_method_types"] = available
        result_methods: list[dict[str, str]] = []
        for method in methods:
            method_id = method["id"]
            params = {
                "deferred_intent[mode]": "subscription",
                "deferred_intent[amount]": "0",
                "deferred_intent[currency]": currency.lower(),
                "deferred_intent[setup_future_usage]": "off_session",
                "currency": currency.lower(),
                "key": active_key,
                "_stripe_version": STRIPE_VERSION,
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "stripe_js_id": str(uuid.uuid4()),
                "locale": locale,
                "type": "deferred_intent",
                "checkout_session_id": session_id,
                "deferred_intent[payment_method_types][0]": method_id,
            }
            if customer_session_secret.startswith("cuss_secret_"):
                params["customer_session_client_secret"] = customer_session_secret
            try:
                elements_response = client.get(
                    f"{STRIPE_API}/v1/elements/sessions",
                    params=params,
                    headers={"accept": "application/json"},
                    timeout=float(timeout),
                )
                elements_status = _safe_status(elements_response)
                if elements_status != 200:
                    result_methods.append(_result_method(method_id, method["label"], "unknown", f"Stripe elements HTTP {elements_status or 'network'}"))
                    continue
                element_types = parse_payment_method_types(_json_response(elements_response))
                if method_id in element_types:
                    result_methods.append(_result_method(method_id, method["label"], "supported", "Stripe Elements 返回该方式"))
                else:
                    result_methods.append(_result_method(method_id, method["label"], "unsupported", "Stripe Elements 未返回该方式"))
            except Exception as exc:
                result_methods.append(_result_method(method_id, method["label"], "unknown", f"{type(exc).__name__}"))
        output["methods"] = result_methods
        output["ok"] = True
        output["status"] = "success"
        return output
    except Exception as exc:
        output["error"] = f"{type(exc).__name__}"
        return output
    finally:
        if env is not None:
            try:
                env.close()
            except Exception:
                try:
                    _http_client(env).close()
                except Exception:
                    pass


__all__ = [
    "TRIAL_PAYMENT_METHODS",
    "parse_payment_method_types",
    "probe_trial_payment_methods",
]
