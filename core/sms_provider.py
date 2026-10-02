# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - SMSBower：GET handler_api 兼容接口，文档 https://smsbower.app/cn/api?page=client
    - L：本地 JSON 管理接口，文档 L_API.md
    - H：本地 JSON 管理接口，文档 H_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活。
"""
import json
import logging
import random
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

# GrizzlySMS 规则：号码取出后 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}
_STATE_LOCK = threading.RLock()
_ACTIVATION_STATE: dict[str, dict] = {}
_CODE_HISTORY: dict[str, set[str]] = {}
_RECENT_REJECTED_NUMBERS: dict[str, float] = {}
_TIER_FAILURES: dict[tuple[str, str, str, str], int] = {}
_TIER_COOLDOWNS: dict[tuple[str, str, str, str], float] = {}
_METRICS: dict[str, int] = {}


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsProviderConfigurationError(SmsProviderError):
    """Provider 授权或地址配置错误，重复换号没有意义。"""


class SmsNumberRejectedError(SmsProviderError):
    """号码已被当前目标流程或本地质量策略排除。"""


class SmsCodeRejectedError(SmsProviderError):
    """目标拒绝了当前短信验证码。"""


class SmsNoNumbersError(SmsProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsCodeTimeout(SmsProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


def _http() -> CurlSession:
    s = CurlSession(impersonate=IMPERSONATE)
    s.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return s


def _provider() -> str:
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _setting_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(float(getattr(_cfg, name, default) or default))
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _csv_values(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _mask_phone(phone: str) -> str:
    digits = _normalize_phone_digits(phone)
    if len(digits) <= 4:
        return digits or "-"
    return "*" * max(len(digits) - 4, 2) + digits[-4:]


def _metric(event: str, state: dict | None = None) -> None:
    state = state or {}
    provider = str(state.get("provider") or _provider())
    service = str(state.get("service") or getattr(_cfg, "SMS_SERVICE", "") or "-")
    country = str(state.get("country") or getattr(_cfg, "SMS_COUNTRY", "") or "-")
    tier = str(state.get("provider_id") or state.get("tier") or "default")
    with _STATE_LOCK:
        _METRICS[event] = _METRICS.get(event, 0) + 1
    logger.info(
        "[SMS_METRIC] event=%s provider=%s service=%s country=%s tier=%s",
        event, provider, service, country, tier,
    )


def _prune_strategy_state() -> None:
    now = time.monotonic()
    with _STATE_LOCK:
        for phone, expires_at in list(_RECENT_REJECTED_NUMBERS.items()):
            if expires_at <= now:
                _RECENT_REJECTED_NUMBERS.pop(phone, None)
        for key, expires_at in list(_TIER_COOLDOWNS.items()):
            if expires_at <= now:
                _TIER_COOLDOWNS.pop(key, None)


def _tier_key(state: dict) -> tuple[str, str, str, str]:
    return (
        str(state.get("provider") or _provider()),
        str(state.get("service") or getattr(_cfg, "SMS_SERVICE", "") or ""),
        str(state.get("country") or getattr(_cfg, "SMS_COUNTRY", "") or ""),
        str(state.get("provider_id") or state.get("tier") or "default"),
    )


def _tier_cooldown_key(state: dict) -> tuple[str, str, str, str] | None:
    key = _tier_key(state)
    if key[0] == "smsbower" and key[3].strip().lower() in ("", "default", "unknown"):
        return None
    return key


def _cooldown_provider_ids(service: str, country: str) -> set[str]:
    _prune_strategy_state()
    now = time.monotonic()
    with _STATE_LOCK:
        return {
            key[3]
            for key, expires_at in _TIER_COOLDOWNS.items()
            if key[0] == "smsbower"
            and key[1] == str(service or "")
            and key[2] == str(country or "")
            and expires_at > now
            and key[3] not in ("", "default", "unknown")
        }


def _tier_is_cooled(state: dict) -> bool:
    _prune_strategy_state()
    key = _tier_cooldown_key(state)
    if key is None:
        return False
    with _STATE_LOCK:
        return _TIER_COOLDOWNS.get(key, 0) > time.monotonic()


def _remember_activation(activation_id: str, phone: str, metadata: dict | None = None) -> tuple[str, str]:
    metadata = dict(metadata or {})
    state = {
        "activation_id": str(activation_id),
        "phone_number": str(phone),
        "provider": str(metadata.pop("provider", _provider())),
        "service": str(metadata.pop("service", getattr(_cfg, "SMS_SERVICE", "") or "")),
        "country": str(metadata.pop("country", getattr(_cfg, "SMS_COUNTRY", "") or "")),
        **metadata,
    }
    with _STATE_LOCK:
        _ACTIVATION_STATE[str(activation_id)] = state
        _ACQUIRED_AT[str(activation_id)] = time.time()
    _metric("acquired", state)
    logger.info(
        "[SMS] 激活登记：id=%s provider=%s country=%s tier=%s phone=%s",
        activation_id, state["provider"], state["country"],
        state.get("provider_id") or state.get("tier") or "default", _mask_phone(phone),
    )
    return str(activation_id), str(phone)


def _activation_state(activation_id: str) -> dict:
    with _STATE_LOCK:
        return dict(_ACTIVATION_STATE.get(str(activation_id)) or {})


def _forget_activation(activation_id: str) -> None:
    activation_id = str(activation_id or "")
    with _STATE_LOCK:
        _ACTIVATION_STATE.pop(activation_id, None)
        _CODE_HISTORY.pop(activation_id, None)
        _ACQUIRED_AT.pop(activation_id, None)


def _remember_code(activation_id: str, code: str) -> bool:
    code = str(code or "").strip()
    if not code:
        return False
    activation_id = str(activation_id)
    with _STATE_LOCK:
        history = _CODE_HISTORY.setdefault(activation_id, set())
        if code in history:
            state = dict(_ACTIVATION_STATE.get(activation_id) or {})
            duplicate = True
        else:
            history.add(code)
            state = dict(_ACTIVATION_STATE.get(activation_id) or {})
            duplicate = False
    if duplicate:
        _metric("historical_code", state)
        logger.info("[SMS] 忽略激活 %s 的历史验证码", activation_id)
        return False
    _metric("code_received", state)
    return True


def _record_rejected_number(state: dict, reason: str) -> None:
    phone = _normalize_phone_digits(str(state.get("phone_number") or ""))
    if not phone:
        return
    ttl = _setting_int("SMS_NUMBER_REJECT_TTL", 1800, 60)
    with _STATE_LOCK:
        _RECENT_REJECTED_NUMBERS[phone] = time.monotonic() + ttl
    _metric("number_rejected", state)
    logger.info(
        "[SMS] 记录号码质量失败：phone=%s reason=%s ttl=%ss",
        _mask_phone(phone), reason[:120], ttl,
    )


def _record_tier_failure(state: dict, category: str) -> None:
    key = _tier_cooldown_key(state)
    if key is None:
        logger.info(
            "[SMS] 跳过 Provider/tier 冷却统计：provider=%s tier=default category=%s",
            state.get("provider") or _provider(), category,
        )
        return
    threshold = _setting_int("SMS_TIER_FAILURE_THRESHOLD", 2, 1)
    cooldown_seconds = _setting_int("SMS_TIER_COOLDOWN_SECONDS", 45 * 60, 60)
    with _STATE_LOCK:
        count = _TIER_FAILURES.get(key, 0) + 1
        _TIER_FAILURES[key] = count
        if count >= threshold:
            _TIER_COOLDOWNS[key] = time.monotonic() + cooldown_seconds
            metric_state = dict(state)
        else:
            metric_state = None
    if metric_state is not None:
        _metric("tier_cooldown", metric_state)
        logger.warning(
            "[SMS] Provider/tier 进入冷却：key=%s failures=%s seconds=%s category=%s",
            key, count, cooldown_seconds, category,
        )


def _record_success(activation_id: str) -> None:
    state = _activation_state(activation_id)
    if state:
        key = _tier_cooldown_key(state)
        if key is not None:
            with _STATE_LOCK:
                _TIER_FAILURES.pop(key, None)
                _TIER_COOLDOWNS.pop(key, None)
    _metric("success", state)


def _classify_failure(reason: object) -> str:
    if isinstance(reason, SmsProviderConfigurationError):
        return "provider_config"
    if isinstance(reason, SmsNoBalanceError):
        return "no_balance"
    if isinstance(reason, SmsNoNumbersError):
        return "no_inventory"
    if isinstance(reason, SmsCodeTimeout):
        return "code_timeout"
    if isinstance(reason, SmsCodeRejectedError):
        return "code_rejected"
    text = str(reason or "").strip().lower()
    if any(marker in text for marker in (
        "admin_auth_code", "api key", "api_key", "bad_key", "http 401", "http 403",
        "api_base 不能为空", "api key 不能为空",
    )):
        return "provider_config"
    if any(marker in text for marker in (
        "no_numbers", "no numbers", "暂无可用号码", "没有可用号码", "库存为空",
    )):
        return "no_inventory"
    if any(marker in text for marker in ("no_balance", "no_money", "余额不足", "insufficient balance")):
        return "no_balance"
    if any(marker in text for marker in (
        "invalid_phone_code", "invalid code", "incorrect code", "wrong code", "expired code",
        "验证码无效", "验证码错误", "验证码已过期",
    )):
        return "code_rejected"
    if any(marker in text for marker in (
        "invalid_phone", "not a valid phone", "phone number is not valid", "phone_in_use",
        "already used", "voip", "号码无效", "手机号无效", "手机号已被使用", "whatsapp_channel",
    )):
        return "number_rejected"
    if any(marker in text for marker in (
        "send_not_accepted", "delivery_refused", "cannot send", "could not send",
        "unable to send", "failed to send", "send failed", "send_limited", "限流", "发送失败",
    )):
        return "send_failed"
    if any(marker in text for marker in ("timeout", "timed out", "超时")):
        return "code_timeout"
    if "status_cancel" in text or "已被取消" in text:
        return "activation_cancelled"
    return "unknown"


def _report_failure_with_state(
    activation_id: str,
    reason: object = "",
    category: str | None = None,
    state: dict | None = None,
) -> str:
    state = state if state is not None else _activation_state(activation_id)
    category = category or _classify_failure(reason)
    if category == "provider_config":
        _metric("provider_config_error", state)
    elif category == "no_balance":
        _metric("no_balance", state)
    elif category == "no_inventory":
        _metric("no_inventory", state)
    elif category in ("number_rejected", "send_failed", "code_rejected", "code_timeout"):
        if state and category in ("number_rejected", "send_failed", "code_rejected"):
            _record_rejected_number(state, str(reason or category))
        if state:
            _record_tier_failure(state, category)
        _metric(category, state)
    else:
        _metric(category, state)
    logger.info(
        "[SMS] 记录激活反馈：id=%s category=%s reason=%s",
        activation_id, category, str(reason or "")[:180],
    )
    return category


def report_failure(activation_id: str, reason: object = "", category: str | None = None) -> str:
    """接收注册层反馈，只更新短信调度状态，不改变页面流程。"""
    return _report_failure_with_state(activation_id, reason=reason, category=category)


def cancel_and_report_failure(
    activation_id: str,
    http: CurlSession | None = None,
    reason: object = "",
    category: str | None = None,
) -> str:
    """先发起号码释放，再用释放前快照记录失败状态。"""
    state = _activation_state(activation_id)
    cancel(activation_id, http=http)
    return _report_failure_with_state(activation_id, reason=reason, category=category, state=state)


def report_success(activation_id: str) -> None:
    """记录成功反馈，清除该 Provider/tier 的连续失败计数。"""
    _record_success(activation_id)


def get_sms_runtime_metrics() -> dict:
    """返回脱敏的进程级短信指标，供健康检查和单测使用。"""
    _prune_strategy_state()
    with _STATE_LOCK:
        return {
            "events": dict(_METRICS),
            "active_activations": len(_ACTIVATION_STATE),
            "rejected_numbers": len(_RECENT_REJECTED_NUMBERS),
            "cooled_tiers": len(_TIER_COOLDOWNS),
        }


def _reset_runtime_state_for_tests() -> None:
    with _STATE_LOCK:
        _ACTIVATION_STATE.clear()
        _CODE_HISTORY.clear()
        _RECENT_REJECTED_NUMBERS.clear()
        _TIER_FAILURES.clear()
        _TIER_COOLDOWNS.clear()
        _METRICS.clear()
        _ACQUIRED_AT.clear()


def _request_grizzly(http: CurlSession, params: dict) -> str:
    """
    发一个 GrizzlySMS API 请求，返回去空白的响应文本。
    统一识别公共错误码并抛对应异常。
    """
    if not str(getattr(_cfg, "SMS_API_KEY", "") or "").strip():
        raise SmsProviderConfigurationError("SMS_API_KEY 不能为空")
    base = str(getattr(_cfg, "SMS_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderConfigurationError("SMS_API_BASE 不能为空")
    base_params = {"api_key": _cfg.SMS_API_KEY}
    base_params.update(params)
    resp = http.get(base, params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"GrizzlySMS HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsProviderConfigurationError("接码平台 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError("接码平台暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsProviderError("接码平台地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text == "BAD_SERVICE":
        raise SmsProviderConfigurationError(f"接码平台服务代码无效：{text}")
    if text in ("BAD_ACTION", "BAD_STATUS"):
        raise SmsProviderError(f"接码平台请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsProviderError("激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"该服务被平台禁售：{text}")

    return text


def _request_smsbower(http: CurlSession, params: dict) -> str:
    """发 SMSBower handler_api 请求，返回去空白的响应文本。"""
    api_key = str(getattr(_cfg, "SMSBOWER_API_KEY", "") or "").strip()
    if not api_key:
        raise SmsProviderConfigurationError("SMSBower API Key 不能为空")
    base = str(getattr(_cfg, "SMSBOWER_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderConfigurationError("SMSBOWER_API_BASE 不能为空")
    resp = http.get(base, params={"api_key": api_key, **params})
    text = (resp.text or "").strip()
    if resp.status_code in (401, 403):
        raise SmsProviderConfigurationError(f"SMSBower HTTP {resp.status_code}: 授权失败")
    if resp.status_code != 200:
        raise SmsProviderError(f"SMSBower HTTP {resp.status_code}: {text[:200]}")
    if text in ("BAD_KEY", "BAD_ACTION", "BAD_SERVICE", "WRONG_SERVICE", "BAD_STATUS", "NO_ACTIVATION"):
        if text == "BAD_KEY":
            raise SmsProviderConfigurationError("SMSBower API key 无效（BAD_KEY）")
        if text in ("BAD_SERVICE", "WRONG_SERVICE"):
            raise SmsProviderConfigurationError(f"SMSBower 服务代码无效（{text}），OpenAI/ChatGPT 请填写 dr")
        if text == "NO_ACTIVATION":
            raise SmsProviderError("SMSBower 激活 ID 不存在（NO_ACTIVATION）")
        raise SmsProviderError(f"SMSBower 请求参数错误：{text}")
    if text in ("NO_NUMBERS", "NO_BALANCE", "NO_MONEY"):
        if text in ("NO_BALANCE", "NO_MONEY"):
            raise SmsNoBalanceError(f"SMSBower 余额不足（{text}），请充值")
        raise SmsNoNumbersError("SMSBower 暂无可用号码（NO_NUMBERS）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"SMSBower 该服务被禁售：{text}")
    return text


def _smsbower_price_bound(name: str) -> Decimal | None:
    value = str(getattr(_cfg, name, "") or "").strip()
    if not value and name == "SMSBOWER_MAX_PRICE":
        value = str(getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SmsProviderConfigurationError(f"{name} 必须是有效价格：{value!r}") from exc
    if not parsed.is_finite():
        raise SmsProviderConfigurationError(f"{name} 必须是有限价格：{value!r}")
    if parsed < 0:
        raise SmsProviderConfigurationError(f"{name} 不能小于 0：{value!r}")
    return parsed


def _smsbower_price_bounds_usd() -> tuple[Decimal | None, Decimal | None]:
    min_cny = _smsbower_price_bound("SMSBOWER_MIN_PRICE")
    max_cny = _smsbower_price_bound("SMSBOWER_MAX_PRICE")
    if min_cny is None and max_cny is None:
        return None, None
    if min_cny is not None and max_cny is not None and min_cny > max_cny:
        raise SmsProviderConfigurationError("SMSBOWER_MIN_PRICE 不能大于 SMSBOWER_MAX_PRICE")
    raw_rate = str(getattr(_cfg, "SMSBOWER_USD_CNY_RATE", "7.2") or "").strip()
    try:
        rate = Decimal(raw_rate)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SmsProviderConfigurationError(f"SMSBOWER_USD_CNY_RATE 必须是正数：{raw_rate!r}") from exc
    if not rate.is_finite() or rate <= 0:
        raise SmsProviderConfigurationError(f"SMSBOWER_USD_CNY_RATE 必须是正数：{raw_rate!r}")
    return (
        min_cny / rate if min_cny is not None else None,
        max_cny / rate if max_cny is not None else None,
    )


def _smsbower_api_price(value: Decimal, rounding: str) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=rounding), "f")


def _sms_service_code(service: str | None = None) -> str:
    service_code = str(service or getattr(_cfg, "SMS_SERVICE", "") or "").strip()
    if service_code.lower() in ("openai", "chatgpt"):
        service_code = "dr"
    if not service_code:
        raise SmsProviderConfigurationError("SMS_SERVICE 不能为空")
    return service_code


def _smsbower_price_rows(http: CurlSession, service: str) -> list[dict]:
    """读取 SMSBower 价格/库存表；该接口只查询库存，不申请号码。"""
    try:
        payload = json.loads(_request_smsbower(http, {"action": "getPrices", "service": service}))
    except json.JSONDecodeError as exc:
        raise SmsProviderError("SMSBower getPrices 响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise SmsProviderError("SMSBower getPrices 响应不是 JSON 对象")

    rows: list[dict] = []
    for country_id, services in payload.items():
        if not isinstance(services, dict):
            continue
        row = services.get(service)
        if not isinstance(row, dict):
            continue
        try:
            cost = Decimal(str(row.get("cost")))
            count = int(row.get("count") or 0)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if count > 0 and cost.is_finite() and cost >= 0:
            rows.append({"country": str(country_id), "cost": cost, "count": count})
    return rows


def _activation_price_metadata(
    data: dict | None,
    provider: str,
    price_quote: dict | None = None,
) -> dict:
    sources = []
    if isinstance(data, dict):
        sources.append(data)
        for key in ("item", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                sources.append({**data, **nested})

    def currency_code(value: object) -> str | None:
        text = str(value or "").strip().upper()
        if text in ("USD", "US DOLLAR", "$"):
            return "USD"
        if text in ("CNY", "RMB", "CNH", "¥", "￥"):
            return "CNY"
        return None

    def make_price(value: object, currency: object, source: str) -> dict | None:
        raw = str(value or "").strip()
        detected = currency_code(currency)
        if raw.startswith(("$", "¥", "￥")):
            detected = detected or currency_code(raw[0])
            raw = raw[1:].strip()
        for suffix in ("USD", "CNY", "RMB"):
            if raw.upper().endswith(suffix):
                detected = detected or currency_code(suffix)
                raw = raw[:-len(suffix)].strip()
                break
        if not detected:
            return None
        try:
            amount = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not amount.is_finite() or amount < 0:
            return None
        return {
            "price_amount": format(amount, "f"),
            "price_currency": detected,
            "price_source": source,
        }

    for source in sources:
        for key, currency in (("price_usd", "USD"), ("cost_usd", "USD"), ("price_cny", "CNY"), ("cost_cny", "CNY")):
            value = source.get(key)
            if value is not None:
                result = make_price(value, currency, f"{provider.upper()} 激活响应")
                if result:
                    return result
        currency = next((source.get(key) for key in ("currency", "price_currency", "priceCurrency") if source.get(key)), None)
        if currency is None and provider == "smsbower":
            currency = "USD"
        for key in ("price", "cost"):
            value = source.get(key)
            if value is not None:
                result = make_price(value, currency, f"{provider.upper()} 激活响应")
                if result:
                    return result

    if provider == "smsbower" and isinstance(price_quote, dict):
        result = make_price(price_quote.get("cost"), "USD", "SMSBower getPrices 报价")
        if result:
            return result
    return {}


def _smsbower_random_country_candidates(http: CurlSession, service: str) -> list[dict]:
    """返回有库存且价格在配置区间内的国家，并随机打乱顺序。"""
    if not bool(getattr(_cfg, "SMSBOWER_RANDOM_COUNTRY", True)):
        return []
    min_price_cny = _smsbower_price_bound("SMSBOWER_MIN_PRICE")
    max_price_cny = _smsbower_price_bound("SMSBOWER_MAX_PRICE")
    min_price_usd, max_price_usd = _smsbower_price_bounds_usd()
    if max_price_usd is None:
        logger.warning("[SMSBower] 未配置最高价格，继续使用固定国家 SMS_COUNTRY=%s", getattr(_cfg, "SMS_COUNTRY", ""))
        return []
    candidates = [
        row for row in _smsbower_price_rows(http, service)
        if row["cost"] <= max_price_usd and (min_price_usd is None or row["cost"] >= min_price_usd)
    ]

    random.shuffle(candidates)
    logger.info(
        "[SMSBower] 随机国家候选：service=%s count=%s price_usd=%s..%s price_cny=%s..%s rate=%s",
        service, len(candidates), min_price_usd if min_price_usd is not None else 0,
        max_price_usd, min_price_cny if min_price_cny is not None else 0,
        max_price_cny, getattr(_cfg, "SMSBOWER_USD_CNY_RATE", "7.2"),
    )
    if not candidates:
        raise SmsNoNumbersError(
            f"SMSBower 在人民币最高价格 ¥{max_price_cny}（约 USD {max_price_usd:.6f}）内没有可用国家"
        )
    return candidates


def preflight_sms_dependency(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
) -> dict:
    """在手机流程取号前检查短信依赖，绝不调用消费型取号接口。"""
    provider = _provider()
    service_code = _sms_service_code(service)
    country_code = str(country if country is not None else getattr(_cfg, "SMS_COUNTRY", "") or "").strip()

    if provider == "smsbower":
        own_http = http is None
        http = http or _http()
        try:
            rows = _smsbower_price_rows(http, service_code)
            min_price_cny = _smsbower_price_bound("SMSBOWER_MIN_PRICE")
            max_price_cny = _smsbower_price_bound("SMSBOWER_MAX_PRICE")
            min_price_usd, max_price_usd = _smsbower_price_bounds_usd()
            random_mode = bool(getattr(_cfg, "SMSBOWER_RANDOM_COUNTRY", True)) and country is None and max_price_usd is not None
            if random_mode:
                candidates = [
                    row for row in rows
                    if row["cost"] <= max_price_usd and (min_price_usd is None or row["cost"] >= min_price_usd)
                ]
                if not candidates:
                    raise SmsNoNumbersError(
                        f"SMSBower 在人民币最高价格 ¥{max_price_cny}（约 USD {max_price_usd:.6f}）内没有可用国家"
                    )
                return {
                    "provider": provider,
                    "service": service_code,
                    "country": None,
                    "probe": "getPrices",
                    "available": True,
                    "candidate_count": len(candidates),
                    "countries": [row["country"] for row in candidates],
                }

            if not country_code:
                raise SmsProviderConfigurationError("SMS_COUNTRY 不能为空")
            target = country_code.lstrip("+")
            candidates = [
                row for row in rows
                if row["country"].lstrip("+") == target
                and (max_price_usd is None or row["cost"] <= max_price_usd)
                and (min_price_usd is None or row["cost"] >= min_price_usd)
            ]
            if not candidates:
                raise SmsNoNumbersError(f"SMSBower 国家 {country_code} 暂无符合价格/库存条件的号码")
            return {
                "provider": provider,
                "service": service_code,
                "country": country_code,
                "probe": "getPrices",
                "available": True,
                "candidate_count": len(candidates),
                "countries": [country_code],
            }
        finally:
            if own_http:
                http.close()

    if provider == "l":
        _l_url("/api/admin/l/take-phone")
        _l_headers()
    elif provider == "h":
        _h_url("/api/admin/h/take-phone")
        _h_headers()
    elif provider == "grizzly":
        if not str(getattr(_cfg, "SMS_API_KEY", "") or "").strip():
            raise SmsProviderConfigurationError("SMS_API_KEY 不能为空")
        if not str(getattr(_cfg, "SMS_API_BASE", "") or "").strip():
            raise SmsProviderConfigurationError("SMS_API_BASE 不能为空")
    else:
        raise SmsProviderConfigurationError(f"未知 SMS_PROVIDER：{provider}")

    if not country_code:
        raise SmsProviderConfigurationError("SMS_COUNTRY 不能为空")
    return {
        "provider": provider,
        "service": service_code,
        "country": country_code,
        "probe": "configuration_only",
        "available": True,
    }


def classify_sms_exception(reason: object) -> dict:
    """把短信依赖异常转换为 Codex/注册任务可持久化的状态。"""
    if isinstance(reason, SmsNoBalanceError):
        status, error_code, retryable = "blocked", "sms_no_balance", False
    elif isinstance(reason, SmsProviderConfigurationError):
        status, error_code, retryable = "blocked", "sms_provider_configuration", False
    elif isinstance(reason, SmsNoNumbersError):
        status, error_code, retryable = "failed", "sms_no_numbers", True
    elif isinstance(reason, SmsCodeTimeout):
        status, error_code, retryable = "failed", "sms_code_timeout", True
    elif isinstance(reason, SmsNumberRejectedError):
        status, error_code, retryable = "failed", "sms_number_rejected", True
    elif isinstance(reason, SmsCodeRejectedError):
        status, error_code, retryable = "failed", "sms_code_rejected", True
    elif isinstance(reason, SmsProviderError):
        status, error_code, retryable = "failed", "sms_provider_error", True
    else:
        status, error_code, retryable = "failed", "sms_error", True
    return {
        "status": status,
        "error_code": error_code,
        "retryable": retryable,
        "message": str(reason or ""),
    }


def _smsbower_number_params(service: str | None, country: str | None) -> dict:
    service_code = str(service or _cfg.SMS_SERVICE or "").strip()
    if service_code.lower() in ("openai", "chatgpt"):
        service_code = "dr"
    params = {
        "action": "getNumberV2" if bool(getattr(_cfg, "SMSBOWER_USE_V2", True)) else "getNumber",
        "service": service_code,
        "country": str(country or _cfg.SMS_COUNTRY or "").strip(),
    }
    min_price_usd, max_price_usd = _smsbower_price_bounds_usd()
    for key, value in (
        ("maxPrice", _smsbower_api_price(max_price_usd, ROUND_DOWN) if max_price_usd is not None else ""),
        ("minPrice", _smsbower_api_price(min_price_usd, ROUND_UP) if min_price_usd is not None else ""),
        ("providerIds", getattr(_cfg, "SMSBOWER_PROVIDER_IDS", "")),
        ("exceptProviderIds", getattr(_cfg, "SMSBOWER_EXCEPT_PROVIDER_IDS", "")),
        ("phoneException", getattr(_cfg, "SMSBOWER_PHONE_EXCEPTION", "")),
    ):
        value = str(value or "").strip()
        if value:
            params[key] = value
    dynamic_excluded = _cooldown_provider_ids(service_code, params.get("country", ""))
    if dynamic_excluded:
        params["exceptProviderIds"] = ",".join(sorted(_csv_values(params.get("exceptProviderIds")) | dynamic_excluded))
    return params


def _request_smsbower_number(http: CurlSession, params: dict) -> tuple[dict, str]:
    """按兼容性顺序取号，筛选无库存时放宽供应商条件。"""
    candidates: list[dict] = [dict(params)]
    if params.get("action") == "getNumberV2":
        candidates.append({**params, "action": "getNumber"})
    if params.get("providerIds"):
        without_provider = {key: value for key, value in params.items() if key != "providerIds"}
        candidates.append(without_provider)
        if params.get("action") == "getNumberV2":
            candidates.append({**without_provider, "action": "getNumber"})
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    for index, candidate in enumerate(unique):
        try:
            return candidate, _request_smsbower(http, candidate)
        except SmsNoNumbersError:
            if index + 1 < len(unique):
                logger.warning("[SMSBower] 取号筛选无库存，放宽条件重试：action=%s providerIds=%s", candidate.get("action"), candidate.get("providerIds", "-"))
                continue
            raise
        except SmsProviderError as exc:
            if candidate.get("action") == "getNumberV2" and "BAD_ACTION" in str(exc) and index + 1 < len(unique):
                logger.warning("[SMSBower] getNumberV2 不被当前接口支持，回退兼容接口")
                continue
            raise
    raise SmsProviderError("SMSBower 没有可用的取号请求方案")


def _l_url(path: str) -> str:
    base = str(getattr(_cfg, "L_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderConfigurationError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers() -> dict:
    token = str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderConfigurationError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_l_url(path), headers=_l_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code in (401, 403):
        raise SmsProviderConfigurationError(f"L HTTP {resp.status_code}: 管理授权失败")
    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _h_url(path: str) -> str:
    base = str(getattr(_cfg, "H_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderConfigurationError("H_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _h_headers() -> dict:
    token = str(getattr(_cfg, "H_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderConfigurationError("H_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_h_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_h_url(path), headers=_h_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code in (401, 403):
        raise SmsProviderConfigurationError(f"H HTTP {resp.status_code}: 管理授权失败")
    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"H HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"H 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"H 暂无可用号码：{combined}")
        raise SmsProviderError(f"H 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"H 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_h_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 H_API /api/admin/h/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("H release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"H release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:H] 已释放号码 id={activation_id}, released={released}")
        _forget_activation(activation_id)
        return data
    finally:
        if own_http:
            http.close()


def release_h_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 H 号码。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("H release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:H] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _forget_activation(activation_id)
        return data
    finally:
        if own_http:
            http.close()


def _release_l_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _forget_activation(activation_id)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _forget_activation(activation_id)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "L_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _normalize_h_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "H_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _h_phone_acquire_mode() -> str:
    """
    H 取号模式：
      - reusable/reuse/prefer_reuse：优先复用，调用 /api/admin/h/take-reusable-phone
      - new/fresh/always_new：每次取新号，调用 /api/admin/h/take-phone
    """
    raw = str(getattr(_cfg, "H_PHONE_ACQUIRE_MODE", "reusable") or "reusable").strip().lower()
    if raw in ("new", "fresh", "always_new", "take_phone", "take-phone", "每次取新号", "新号"):
        return "new"
    return "reusable"


# ============================================================
# 取号
# ============================================================

def _acquire_number_once(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
    price_quote: dict | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "smsbower":
            params, text = _request_smsbower_number(http, _smsbower_number_params(service, country))
            if params["action"] == "getNumberV2":
                try:
                    data = json.loads(text)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    activation_id = str(data.get("activationId") or data.get("id") or "").strip()
                    phone = str(data.get("phoneNumber") or data.get("phone") or "").strip()
                    if activation_id and phone:
                        provider_id = str(
                            data.get("providerId")
                            or data.get("provider_id")
                            or data.get("activationOperator")
                            or data.get("operator")
                            or ""
                        ).strip()
                        tier = str(
                            data.get("activationOperator")
                            or data.get("operator")
                            or data.get("tier")
                            or provider_id
                            or "default"
                        ).strip()
                        metadata = {
                            "provider": "smsbower",
                            "service": str(service or getattr(_cfg, "SMS_SERVICE", "") or ""),
                            "country": str(country or getattr(_cfg, "SMS_COUNTRY", "") or ""),
                            "tier": tier,
                            "acquire_mode": "getNumberV2",
                        }
                        if provider_id:
                            metadata["provider_id"] = provider_id
                        metadata.update(_activation_price_metadata(data, "smsbower", price_quote))
                        return _remember_activation(activation_id, phone, metadata)
                raise SmsProviderError(f"SMSBower getNumberV2 响应格式异常：{text[:200]}")
            if not text.startswith("ACCESS_NUMBER:"):
                raise SmsProviderError(f"SMSBower getNumber 非预期响应：{text[:200]}")
            parts = text.split(":", 2)
            if len(parts) < 3:
                raise SmsProviderError(f"SMSBower getNumber 响应格式异常：{text[:200]}")
            activation_id, phone = parts[1].strip(), parts[2].strip()
            return _remember_activation(
                activation_id,
                phone,
                {
                    "provider": "smsbower",
                    "service": str(service or getattr(_cfg, "SMS_SERVICE", "") or ""),
                    "country": str(country or getattr(_cfg, "SMS_COUNTRY", "") or ""),
                    "tier": "default",
                    "acquire_mode": "getNumber",
                    **_activation_price_metadata(None, "smsbower", price_quote),
                },
            )

        if _provider() == "l":
            payload = {
                "service": service or _cfg.SMS_SERVICE,
                "country": country or _cfg.SMS_COUNTRY,
            }
            if _cfg.SMS_MAX_PRICE:
                payload["maxPrice"] = _cfg.SMS_MAX_PRICE

            data = _post_l_json(http, "/api/admin/l/take-phone", payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "L_PHONE_PREFIX", "") or "")
            phone = _normalize_l_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return _remember_activation(
                activation_id,
                phone,
                {
                    "provider": "l",
                    "service": str(service or getattr(_cfg, "SMS_SERVICE", "") or ""),
                    "country": str(country or getattr(_cfg, "SMS_COUNTRY", "") or ""),
                    "tier": "default",
                    "acquire_mode": "take-phone",
                    **_activation_price_metadata(data, "l"),
                },
            )

        if _provider() == "h":
            # H_API 使用 projectId + country；统一复用 SMS_SERVICE / SMS_COUNTRY，
            # 避免接码平台之间出现重复的“服务/国家”配置。
            project_id = str(service or _cfg.SMS_SERVICE).strip()
            h_country = str(country or _cfg.SMS_COUNTRY).strip()
            if not project_id:
                raise SmsProviderError("H projectId 不能为空：请填写 SMS_SERVICE")
            if not h_country:
                raise SmsProviderError("H country 不能为空：请填写 SMS_COUNTRY")
            payload = {
                "projectId": project_id,
                "country": h_country,
            }
            mode = _h_phone_acquire_mode()
            api_path = "/api/admin/h/take-phone" if mode == "new" else "/api/admin/h/take-reusable-phone"
            data = _post_h_json(http, api_path, payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "H_PHONE_PREFIX", "") or "")
            phone = _normalize_h_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:H] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"H {api_path.rsplit('/', 1)[-1]} 响应缺少 item.id/item.phone：{str(data)[:200]}")
            logger.info(
                f"[SMS:H] 取号成功：mode={mode}, api={api_path}, id={activation_id}, phone=+{phone}, "
                f"reused={bool(data.get('reused'))}, duplicate={bool(data.get('duplicate'))}"
            )
            return _remember_activation(
                activation_id,
                phone,
                {
                    "provider": "h",
                    "service": project_id,
                    "country": h_country,
                    "tier": "default",
                    "acquire_mode": mode,
                    "reused": bool(data.get("reused")),
                    "duplicate": bool(data.get("duplicate")),
                    **_activation_price_metadata(data, "h"),
                },
            )

        params = {
            "action": "getNumber",
            "service": service or _cfg.SMS_SERVICE,
            "country": country or _cfg.SMS_COUNTRY,
        }
        if _cfg.SMS_MAX_PRICE:
            params["maxPrice"] = _cfg.SMS_MAX_PRICE

        text = _request_grizzly(http, params)
        # 成功格式：ACCESS_NUMBER:激活ID:号码
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsProviderError(f"getNumber 非预期响应：{text[:200]}")
        parts = text.split(":")
        if len(parts) < 3:
            raise SmsProviderError(f"getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = parts[2].strip()
        logger.info(f"[SMS] 取号成功：activation_id={activation_id}, phone=+{phone}")
        return _remember_activation(
            activation_id,
            phone,
            {
                "provider": "grizzly",
                "service": str(service or getattr(_cfg, "SMS_SERVICE", "") or ""),
                "country": str(country or getattr(_cfg, "SMS_COUNTRY", "") or ""),
                "tier": "default",
                "acquire_mode": "getNumber",
            },
        )
    finally:
        if own_http:
            http.close()


def _number_is_recently_rejected(phone: str) -> bool:
    normalized = _normalize_phone_digits(phone)
    if not normalized:
        return False
    _prune_strategy_state()
    with _STATE_LOCK:
        return _RECENT_REJECTED_NUMBERS.get(normalized, 0) > time.monotonic()


def _release_strategy_rejected(activation_id: str, http: CurlSession) -> None:
    provider = _provider()
    try:
        cancel(activation_id, http=http, background=(provider == "grizzly"))
    finally:
        if provider != "grizzly":
            _forget_activation(activation_id)


def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
) -> tuple[str, str]:
    """取号兼容入口；SMSBower 默认从最高价格内的有库存国家随机选择。"""
    own_http = http is None
    http = http or _http()
    attempts = _setting_int("SMS_NUMBER_ACQUIRE_RETRIES", 3, 1)
    service_code = str(service or getattr(_cfg, "SMS_SERVICE", "") or "").strip()
    if service_code.lower() in ("openai", "chatgpt"):
        service_code = "dr"
    random_countries: list[dict] = []
    fixed_price_quote = None
    if _provider() == "smsbower" and country is None:
        random_countries = _smsbower_random_country_candidates(http, service_code)
        attempts = min(
            max(1, _setting_int("SMSBOWER_RANDOM_COUNTRY_ATTEMPTS", 12, 1)),
            max(1, len(random_countries)),
        )
    if _provider() == "smsbower" and not random_countries:
        configured_country = str(country or getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
        if configured_country:
            try:
                fixed_price_quote = next(
                    (row for row in _smsbower_price_rows(http, service_code) if row["country"] == configured_country),
                    None,
                )
            except Exception as exc:
                logger.warning("[SMSBower] 获取号码价格快照失败，不影响取号：%s", exc)
    try:
        for attempt in range(1, attempts + 1):
            candidate = random_countries[attempt - 1] if random_countries else None
            selected_country = candidate["country"] if candidate else country
            try:
                activation_id, phone = _acquire_number_once(
                    http, service=service_code or service, country=selected_country,
                    price_quote=candidate or fixed_price_quote,
                )
            except SmsNoNumbersError:
                if random_countries and attempt < attempts:
                    candidate = random_countries[attempt - 1]
                    logger.info(
                        "[SMSBower] 随机国家无号，继续候选：attempt=%s/%s country=%s cost=%s",
                        attempt, attempts, candidate["country"], candidate["cost"],
                    )
                    continue
                raise
            state = _activation_state(activation_id)
            if not state:
                state = {
                    "activation_id": activation_id,
                    "phone_number": phone,
                    "provider": _provider(),
                    "service": service_code,
                    "country": str(selected_country or getattr(_cfg, "SMS_COUNTRY", "") or ""),
                }
                _remember_activation(activation_id, phone, state)
            recently_rejected = _number_is_recently_rejected(phone)
            cooled = _tier_is_cooled(state)
            if recently_rejected or cooled:
                reason = "recently_rejected_number" if recently_rejected else "cooled_tier"
                _metric("candidate_skipped", state)
                logger.info("[SMS] 跳过策略排除候选：attempt=%s/%s reason=%s phone=%s", attempt, attempts, reason, _mask_phone(phone))
                _release_strategy_rejected(activation_id, http)
                continue
            return activation_id, phone
        if random_countries:
            raise SmsNoNumbersError(f"随机尝试 {attempts} 个价格范围内国家后仍无可用号码")
        raise SmsNumberRejectedError(f"连续 {attempts} 次取到的号码或 Provider tier 均被短信策略排除")
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    own_http = http is None
    http = http or _http()
    total_wait = max_wait if max_wait is not None else _setting_int("SMS_CODE_WAIT", 120, 0)
    interval = poll_interval if poll_interval is not None else _setting_int("SMS_POLL_INTERVAL", 5, 0)
    deadline = time.monotonic() + total_wait
    retry_status_sent = False
    try:
        provider = _provider()
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.monotonic() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.monotonic())))
            remaining_before = max(0, int(deadline - time.monotonic()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code and _remember_code(activation_id, code):
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    return code
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮忽略历史验证码：{code}")
                remaining = max(0, int(deadline - time.monotonic()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "h":
                data = _post_h_json(http, "/api/admin/h/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code and _remember_code(activation_id, code):
                    logger.info(f"[SMS:H] 第 {round_no} 轮收到验证码：{code}")
                    return code
                if code:
                    logger.info(f"[SMS:H] 第 {round_no} 轮忽略历史验证码：{code}")
                remaining = max(0, int(deadline - time.monotonic()))
                logger.info(
                    f"[SMS:H] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "smsbower":
                text = _request_smsbower(http, {"action": "getStatus", "id": activation_id})
                if text.startswith("STATUS_OK:"):
                    code = text.split(":", 1)[1].strip().strip("'")
                    if _remember_code(activation_id, code):
                        logger.info(f"[SMSBower] 第 {round_no} 轮收到验证码：{code}")
                        return code
                    logger.info(f"[SMSBower] 第 {round_no} 轮忽略历史验证码：{code}")
                elif text == "STATUS_CANCEL":
                    raise SmsProviderError("SMSBower 激活已被取消（STATUS_CANCEL）")
                elif text.startswith("STATUS_WAIT_RETRY"):
                    old_code = text.split(":", 1)[1].strip() if ":" in text else ""
                    if old_code:
                        if _remember_code(activation_id, old_code):
                            logger.info(f"[SMSBower] 第 {round_no} 轮记录待重试旧验证码：{old_code}")
                        else:
                            logger.info(f"[SMSBower] 第 {round_no} 轮旧验证码已记录：{old_code}")
                    if not retry_status_sent:
                        retry_status_sent = True
                        try:
                            set_status(activation_id, 3, http=http)
                        except Exception as exc:
                            logger.warning(f"[SMSBower] 请求下一条短信失败（继续轮询）：{exc}")
                time.sleep(interval)
                continue

            text = _request_grizzly(http, {"action": "getStatus", "id": activation_id})

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                if _remember_code(activation_id, code):
                    logger.info(f"[SMS] 第 {round_no} 轮收到验证码：{code}")
                    return code
                logger.info(f"[SMS] 第 {round_no} 轮忽略历史验证码：{code}")
            elif text == "STATUS_CANCEL":
                raise SmsProviderError("激活已被取消（STATUS_CANCEL）")
            elif text.startswith("STATUS_WAIT_RETRY"):
                old_code = text.split(":", 1)[1].strip() if ":" in text else ""
                if old_code:
                    if _remember_code(activation_id, old_code):
                        logger.info(f"[SMS] 第 {round_no} 轮记录待重试旧验证码：{old_code}")
                    else:
                        logger.info(f"[SMS] 第 {round_no} 轮旧验证码已记录：{old_code}")
                if not retry_status_sent:
                    retry_status_sent = True
                    try:
                        set_status(activation_id, 3, http=http)
                    except Exception as exc:
                        logger.warning(f"[SMS] 请求下一条短信失败（继续轮询）：{exc}")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.monotonic()))
            logger.info(f"[SMS] 第 {round_no} 轮未收到验证码，状态={text}，{interval}s 后重试（剩余 {remaining}s）")
            time.sleep(interval)

        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            logger.debug(f"[SMS:L] 忽略状态设置 id={activation_id}, status={status}")
            return "OK"
        if _provider() == "smsbower":
            if int(status) == 1:
                return "OK"
            return _request_smsbower(http, {"action": "setStatus", "status": str(status), "id": activation_id})
        return _request_grizzly(http, {"action": "setStatus", "status": str(status), "id": activation_id})
    finally:
        if own_http:
            http.close()


def _completed_activation_info(activation_id: str) -> dict:
    state = _activation_state(activation_id)
    info = {}
    phone = str(state.get("phone_number") or "").strip()
    if phone:
        info["phone_number"] = phone if phone.startswith("+") else "+" + phone
    country = str(state.get("country") or "").strip()
    if country:
        info["country"] = country
    for key in ("price_amount", "price_currency", "price_source"):
        value = str(state.get(key) or "").strip()
        if value:
            info[key] = value
    return info


def complete(activation_id: str, http: CurlSession | None = None) -> dict:
    """标记激活完成并返回可持久化的号码/价格快照。"""
    info = _completed_activation_info(activation_id)
    if _provider() == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _forget_activation(activation_id)
        return info
    if _provider() == "h":
        # H 成功 fetch-code 后后台会自动按多次收码策略重取；这里不 release。
        logger.info(f"[SMS:H] 已完成 id={activation_id}")
        _forget_activation(activation_id)
        return info
    if _provider() == "smsbower":
        try:
            set_status(activation_id, 6, http=http)
        except Exception as exc:
            logger.warning(f"[SMSBower] 标记完成失败（不影响结果）：{exc}")
        finally:
            _forget_activation(activation_id)
        return info
    try:
        set_status(activation_id, 6, http=http)
        logger.info(f"[SMS] 已标记完成 activation_id={activation_id}")
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")
    finally:
        _forget_activation(activation_id)
    return info


def _do_cancel_sync(activation_id: str, http_factory) -> None:
    """实际的同步取消逻辑：等够 2 分钟限制 → 发请求 → 失败重试一次。"""
    acquired_at = _ACQUIRED_AT.get(activation_id)
    if acquired_at is not None:
        elapsed = time.time() - acquired_at
        if elapsed < _MIN_CANCEL_DELAY:
            wait = _MIN_CANCEL_DELAY - elapsed
            logger.info(
                f"[SMS] 取消等待 GrizzlySMS 2 分钟限制：activation_id={activation_id}，"
                f"还需等 {wait:.0f}s..."
            )
            time.sleep(wait)

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己建一个
    http = None
    try:
        http = http_factory()
        for attempt in range(1, 3):
            try:
                set_status(activation_id, 8, http=http)
                logger.info(f"[SMS] 已取消 activation_id={activation_id}")
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"[SMS] 取消失败（{exc}），5s 后重试...")
                    time.sleep(5)
                else:
                    logger.warning(
                        f"[SMS] 取消最终失败（不影响结果，需到平台手动取消）：activation_id={activation_id}, {exc}"
                    )
    finally:
        _forget_activation(activation_id)
        if http is not None:
            try:
                http.close()
            except Exception:
                pass


def cancel(activation_id: str, http: CurlSession | None = None, background: bool = True) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    失败只告警不抛，不影响主流程。
    """
    if _provider() == "l":
        try:
            _release_l_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _forget_activation(activation_id)
        return
    if _provider() == "h":
        try:
            _release_h_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:H] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _forget_activation(activation_id)
        return
    if _provider() == "smsbower":
        try:
            set_status(activation_id, 8, http=http)
        except Exception as exc:
            logger.warning(f"[SMSBower] 释放号码失败（不影响主流程）：{exc}")
        finally:
            _forget_activation(activation_id)
        return

    if not background:
        _do_cancel_sync(activation_id, _http)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, _http),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")
