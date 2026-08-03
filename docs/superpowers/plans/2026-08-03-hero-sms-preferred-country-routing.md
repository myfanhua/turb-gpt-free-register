# HeroSMS Preferred-Country Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users choose preferred SMS countries in WebUI, then acquire the cheapest in-stock HeroSMS number from those countries, switching country after two actual-number failures and stopping after five actual numbers by default.

**Architecture:** Keep provider HTTP parsing in `core/sms_provider.py`, and put per-Codex-task routing state in a new pure module `core/sms_country_router.py`. The Roxy phone loop owns one router instance so failure counters never leak between accounts. WebUI exposes a cached country-catalog endpoint and renders a searchable multi-select while continuing to persist configuration through the existing `/api/config` and `.env` path.

**Tech Stack:** Python 3, `curl_cffi`, Flask, `Decimal`, native JavaScript/HTML/CSS, `unittest`/`pytest`.

---

## File Structure

- Create `core/sms_country_router.py`: pure offer filtering, price ordering, consecutive-failure tracking, temporary country exclusion, and fallback ordering.
- Modify `core/sms_provider.py`: HeroSMS/SMS-Activate country catalog and price retrieval, normalized offers, and short-lived caches.
- Modify `core/roxy_codex_oauth.py`: use one router per phone-verification task; count only successfully acquired numbers; stop immediately on no balance.
- Modify `config/codex.py`: preferred-country settings and five-attempt source default.
- Modify `config/env_loader.py`: allow an explicitly empty preferred-country list.
- Modify `webui/config_editor.py`: expose the two new settings.
- Modify `webui/app.py`: authenticated country-catalog API.
- Modify `webui/templates/index.html`: searchable preferred-country multi-select and status text.
- Create `tests/test_sms_country_router.py`: pure routing policy tests.
- Modify `tests/test_sms_provider_sms_activate.py`: provider catalog/price parsing and configuration tests.
- Create `tests/test_roxy_sms_country_routing.py`: Roxy integration and no-balance behavior.
- Create `tests/test_webui_sms_country_options.py`: country-catalog API tests.
- Create `tests/test_webui_sms_country_template.py`: frontend contract tests.
- Modify `tests/test_config_defaults.py`: explicit empty list and default migration tests.
- Modify `README.md`: document preferred-country behavior and configuration.

---

### Task 1: Configuration Defaults and Persistence

**Files:**
- Modify: `config/codex.py:112-135,171-172`
- Modify: `config/env_loader.py:20-23`
- Modify: `webui/config_editor.py:581-645`
- Modify: `tests/test_config_defaults.py`
- Modify: `tests/test_sms_provider_sms_activate.py`

- [ ] **Step 1: Write failing configuration tests**

Add these assertions to `tests/test_config_defaults.py`:

```python
from config import codex as codex_config
from pathlib import Path


def test_sms_preferred_countries_can_be_explicitly_empty(self):
    old_loaded = env_loader._LOADED
    env_loader._LOADED = True
    namespace = {"SMS_PREFERRED_COUNTRIES": ["33"]}
    try:
        with patch.dict(os.environ, {"SMS_PREFERRED_COUNTRIES": "[]"}, clear=True):
            env_loader.apply_env_overrides(
                namespace,
                {"SMS_PREFERRED_COUNTRIES": "list_str_multiline"},
            )
    finally:
        env_loader._LOADED = old_loaded
    self.assertEqual(namespace["SMS_PREFERRED_COUNTRIES"], [])


def test_sms_routing_defaults(self):
    source = (Path(__file__).resolve().parents[1] / "config" / "codex.py").read_text(encoding="utf-8")
    self.assertEqual(config_editor._parse_value_from_source(source, "SMS_MAX_RETRIES", "int"), 5)
    self.assertEqual(config_editor._parse_value_from_source(source, "SMS_COUNTRY_FAILURE_SWITCH", "int"), 2)
    self.assertEqual(config_editor._parse_value_from_source(source, "SMS_PREFERRED_COUNTRIES", "list_str_multiline"), [])
```

Extend `SmsActivateProviderTests.test_sms_activate_config_is_exposed_in_webui`:

```python
self.assertIn("SMS_PREFERRED_COUNTRIES", fields)
self.assertEqual(fields["SMS_PREFERRED_COUNTRIES"]["type"], "list_str_multiline")
self.assertIn("SMS_COUNTRY_FAILURE_SWITCH", fields)
self.assertEqual(fields["SMS_COUNTRY_FAILURE_SWITCH"]["type"], "int")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_config_defaults.py tests/test_sms_provider_sms_activate.py -q
```

Expected: failures because `SMS_PREFERRED_COUNTRIES` and `SMS_COUNTRY_FAILURE_SWITCH` do not exist and the source default is not five.

- [ ] **Step 3: Add the configuration values and WebUI metadata**

In `config/codex.py`, replace the single-country/default-retry block with:

```python
# 旧版单国家配置。优选列表为空时仍以它作为兼容回退。
SMS_COUNTRY: str = "10"

# HeroSMS/SMS-Activate 动态价格选路国家列表。每行一个 provider country code。
SMS_PREFERRED_COUNTRIES: list[str] = []

# 同一国家连续使用多少个实际号码失败后切换国家。
SMS_COUNTRY_FAILURE_SWITCH: int = 2

# 单个号愿意支付的最高价格（留空=不限）。
SMS_MAX_PRICE: str = ""

# 单个账号手机验证最多取得多少个实际号码。
SMS_MAX_RETRIES: int = 5
```

Add both keys to `apply_env_overrides`:

```python
'SMS_PREFERRED_COUNTRIES': 'list_str_multiline',
'SMS_COUNTRY_FAILURE_SWITCH': 'int',
```

Add `SMS_PREFERRED_COUNTRIES` to `EXPLICIT_EMPTY_LIST_ENV_KEYS` in `config/env_loader.py` so clearing the WebUI selection remains `[]` instead of falling back to the source default.

Add these fields to `webui/config_editor.py` immediately after `SMS_COUNTRY`:

```python
{
    "key": "SMS_PREFERRED_COUNTRIES", "file": "codex.py",
    "type": "list_str_multiline", "group": "接码平台",
    "label": "优选国家",
    "help": "HeroSMS/SMS-Activate 只在已选国家中按实时价格和库存取号；为空时回退到国家代码",
},
{
    "key": "SMS_COUNTRY_FAILURE_SWITCH", "file": "codex.py",
    "type": "int", "group": "接码平台",
    "label": "连续失败换国家",
    "help": "同一国家连续多少个实际号码失败后切换；推荐 2",
},
```

Include both keys in `smsConfigSectionForKey` later in Task 5.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run:

```powershell
python -m pytest tests/test_config_defaults.py tests/test_sms_provider_sms_activate.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the configuration slice**

```powershell
git add config/codex.py config/env_loader.py webui/config_editor.py tests/test_config_defaults.py tests/test_sms_provider_sms_activate.py
git commit -m "feat: configure preferred sms countries"
```

---

### Task 2: HeroSMS Country Catalog and Price Offers

**Files:**
- Modify: `core/sms_provider.py`
- Modify: `tests/test_sms_provider_sms_activate.py`

- [ ] **Step 1: Write failing provider parsing tests**

Add a response object that supports JSON text to `tests/test_sms_provider_sms_activate.py`, then add:

```python
def test_country_catalog_normalizes_sms_activate_response(self):
    http = _Http(['{"33":{"eng":"Colombia","rus":"Колумбия","visible":1},"187":{"eng":"United States","visible":1}}'])
    with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
        codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
    ), patch.object(codex_config, "SMS_API_KEY", "secret"):
        rows = sms_provider.list_country_catalog(http=http, force=True)
    self.assertEqual(rows, [
        {"code": "33", "name": "Colombia"},
        {"code": "187", "name": "United States"},
    ])
    self.assertEqual(http.calls[0]["params"]["action"], "getCountries")


def test_country_offers_filter_requested_countries_and_service(self):
    http = _Http(['{"33":{"dr":{"cost":0.11,"count":7}},"187":{"dr":{"cost":0.19,"count":3}},"6":{"dr":{"cost":0.08,"count":9}}}'])
    with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
        codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
    ), patch.object(codex_config, "SMS_API_KEY", "secret"):
        offers = sms_provider.get_country_offers(["33", "187"], service="dr", http=http, force=True)
    self.assertEqual([(x.country_code, str(x.price), x.available_count) for x in offers], [
        ("33", "0.11", 7),
        ("187", "0.19", 3),
    ])
    self.assertEqual(http.calls[0]["params"]["action"], "getPrices")
    self.assertEqual(http.calls[0]["params"]["service"], "dr")


def test_country_offers_reject_non_sms_activate_provider(self):
    with patch.object(codex_config, "SMS_PROVIDER", "l"):
        with self.assertRaises(sms_provider.SmsProviderError):
            sms_provider.get_country_offers(["33"])
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py -q
```

Expected: failures because the catalog and offer functions/dataclass are missing.

- [ ] **Step 3: Implement normalized catalog/offers with short caches**

In `core/sms_provider.py`, add imports and model:

```python
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class SmsCountryOffer:
    country_code: str
    price: Decimal
    available_count: int
```

Add module caches guarded by a lock:

```python
_PRICE_CACHE_SECONDS = 30.0
_COUNTRY_CACHE_SECONDS = 3600.0
_CATALOG_CACHE: tuple[float, list[dict]] | None = None
_OFFER_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, list[SmsCountryOffer]]] = {}
_CACHE_LOCK = threading.Lock()
```

Implement these provider-only functions:

```python
def _require_sms_activate_compatible() -> None:
    if _provider() not in {"grizzly", "sms_activate"}:
        raise SmsProviderError("当前接码通道不支持国家价格查询")


def _handler_json(http: CurlSession, params: dict) -> dict:
    text = _request_handler(http, params)
    try:
        data = json.loads(text)
    except Exception as exc:
        raise SmsProviderError(f"接码平台返回无效 JSON：{text[:200]}") from exc
    if not isinstance(data, dict):
        raise SmsProviderError("接码平台 JSON 响应不是对象")
    return data


def list_country_catalog(http=None, *, force=False) -> list[dict]:
    _require_sms_activate_compatible()
    global _CATALOG_CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CATALOG_CACHE
        if cached and not force and now - cached[0] < _COUNTRY_CACHE_SECONDS:
            return [dict(x) for x in cached[1]]
    own_http = http is None
    http = http or _http()
    try:
        data = _handler_json(http, {"action": "getCountries"})
        rows = []
        for code, raw in data.items():
            item = raw if isinstance(raw, dict) else {}
            if item.get("visible") in (False, 0, "0"):
                continue
            name = item.get("eng") or item.get("name") or item.get("country") or item.get("rus") or code
            rows.append({"code": str(code), "name": str(name)})
        rows.sort(key=lambda x: (x["name"].casefold(), x["code"]))
        with _CACHE_LOCK:
            _CATALOG_CACHE = (now, [dict(x) for x in rows])
        return rows
    except Exception:
        with _CACHE_LOCK:
            cached = _CATALOG_CACHE
        if cached:
            logger.warning("[SMS] 国家目录刷新失败，使用最近缓存")
            return [dict(x) for x in cached[1]]
        raise
    finally:
        if own_http:
            http.close()


def get_country_offers(countries, service=None, http=None, *, force=False) -> list[SmsCountryOffer]:
    _require_sms_activate_compatible()
    wanted = list(dict.fromkeys(str(x).strip() for x in countries if str(x).strip()))
    service = str(service or _cfg.SMS_SERVICE).strip()
    key = (str(_cfg.SMS_API_BASE), service, tuple(wanted))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _OFFER_CACHE.get(key)
        if cached and not force and now - cached[0] < _PRICE_CACHE_SECONDS:
            return list(cached[1])
    own_http = http is None
    http = http or _http()
    try:
        data = _handler_json(http, {"action": "getPrices", "service": service})
        offers = []
        for code in wanted:
            services = data.get(code) if isinstance(data.get(code), dict) else {}
            raw = services.get(service) if isinstance(services.get(service), dict) else {}
            try:
                price = Decimal(str(raw.get("cost")))
                count = int(raw.get("count", 0))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price < 0 or count < 0:
                continue
            offers.append(SmsCountryOffer(code, price, count))
        with _CACHE_LOCK:
            _OFFER_CACHE[key] = (now, list(offers))
        return offers
    except Exception:
        with _CACHE_LOCK:
            cached = _OFFER_CACHE.get(key)
        if cached:
            logger.warning("[SMS] 国家报价刷新失败，使用最近缓存")
            return list(cached[1])
        raise
    finally:
        if own_http:
            http.close()
```

The concrete implementations must close only internally-created HTTP sessions, return defensive copies of cached lists, and never log the API key. Keep the most recent valid offer snapshot after its fresh-cache window expires: when a refresh fails, return that stale snapshot with a warning; raise the provider error only when no prior snapshot exists. This provides the design's “recent quote, then saved-order fallback” behavior.

- [ ] **Step 4: Add cache behavior tests**

Add tests proving a second identical call does not issue another HTTP request and `force=True` does. Clear `_CATALOG_CACHE` and `_OFFER_CACHE` in `setUp` to prevent test leakage:

```python
def setUp(self):
    sms_provider._CATALOG_CACHE = None
    sms_provider._OFFER_CACHE.clear()
```

- [ ] **Step 5: Run provider tests and verify they pass**

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py -q
```

Expected: all provider tests pass.

- [ ] **Step 6: Commit provider pricing support**

```powershell
git add core/sms_provider.py tests/test_sms_provider_sms_activate.py
git commit -m "feat: query hero sms country prices"
```

---

### Task 3: Per-Task Preferred-Country Router

**Files:**
- Create: `core/sms_country_router.py`
- Create: `tests/test_sms_country_router.py`

- [ ] **Step 1: Write failing pure routing tests**

Create `tests/test_sms_country_router.py` with a helper and these cases:

```python
from decimal import Decimal
import unittest

from core.sms_country_router import PreferredCountrySelector, NoEligibleSmsCountry
from core.sms_provider import SmsCountryOffer


def offer(code, price, count=1):
    return SmsCountryOffer(code, Decimal(str(price)), count)


class PreferredCountrySelectorTests(unittest.TestCase):
    def selector(self, countries=("33", "187", "6"), max_price="0.20"):
        return PreferredCountrySelector(
            preferred_countries=list(countries),
            fallback_country="33",
            failure_switch=2,
            max_price=max_price,
        )

    def test_selects_cheapest_in_stock_preferred_country(self):
        s = self.selector()
        chosen = s.choose([offer("33", "0.11"), offer("187", "0.19"), offer("6", "0.08")])
        self.assertEqual(chosen, "6")

    def test_equal_prices_use_saved_preference_order(self):
        s = self.selector(countries=("187", "33"))
        self.assertEqual(s.choose([offer("33", "0.10"), offer("187", "0.10")]), "187")

    def test_first_failure_reuses_country_second_failure_switches(self):
        s = self.selector(countries=("33", "187"))
        offers = [offer("33", "0.10"), offer("187", "0.11")]
        self.assertEqual(s.choose(offers), "33")
        s.record_number_failure("33")
        self.assertEqual(s.choose(offers), "33")
        s.record_number_failure("33")
        self.assertEqual(s.choose(offers), "187")

    def test_five_failures_route_a_a_b_b_a(self):
        s = self.selector(countries=("33", "187"))
        offers = [offer("33", "0.10"), offer("187", "0.11")]
        route = []
        for _ in range(5):
            country = s.choose(offers)
            route.append(country)
            s.record_number_failure(country)
        self.assertEqual(route, ["33", "33", "187", "187", "33"])

    def test_no_numbers_skips_country_without_incrementing_failures(self):
        s = self.selector(countries=("33", "187"))
        offers = [offer("33", "0.10"), offer("187", "0.11")]
        self.assertEqual(s.choose(offers), "33")
        s.record_no_numbers("33")
        self.assertEqual(s.failure_count("33"), 0)
        self.assertEqual(s.choose(offers), "187")

    def test_over_price_and_zero_stock_are_excluded(self):
        s = self.selector(countries=("33", "187"), max_price="0.12")
        with self.assertRaises(NoEligibleSmsCountry):
            s.choose([offer("33", "0.13"), offer("187", "0.11", count=0)])

    def test_empty_preferred_list_uses_legacy_country(self):
        s = PreferredCountrySelector([], fallback_country="33", failure_switch=2, max_price="")
        self.assertEqual(s.preferred_countries, ["33"])
```

- [ ] **Step 2: Run the test and verify it fails**

```powershell
python -m pytest tests/test_sms_country_router.py -q
```

Expected: import failure because `core.sms_country_router` does not exist.

- [ ] **Step 3: Implement the pure selector**

Create `core/sms_country_router.py` with these public contracts:

```python
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from core.sms_provider import SmsCountryOffer


class NoEligibleSmsCountry(RuntimeError):
    def __init__(self, reasons: dict[str, str]):
        self.reasons = reasons
        detail = "；".join(f"{code}={reason}" for code, reason in reasons.items())
        super().__init__(f"优选国家当前没有可取号码：{detail}")


class PreferredCountrySelector:
    def __init__(self, preferred_countries, *, fallback_country, failure_switch=2, max_price=""):
        values = list(dict.fromkeys(str(x).strip() for x in preferred_countries if str(x).strip()))
        fallback = str(fallback_country or "").strip()
        self.preferred_countries = values or ([fallback] if fallback else [])
        self.failure_switch = max(1, int(failure_switch or 2))
        try:
            self.max_price = Decimal(str(max_price)) if str(max_price or "").strip() else None
        except InvalidOperation:
            self.max_price = None
        self._failures = {code: 0 for code in self.preferred_countries}
        self._failure_blocked = set()
        self._no_numbers = set()
        self.current_country = None
        self.needs_offer_refresh = True
        self.last_reason = ""

    def choose(self, offers: list[SmsCountryOffer] | None, *, allow_order_fallback=False) -> str:
        order = {code: i for i, code in enumerate(self.preferred_countries)}
        reasons = {}
        offer_map = {x.country_code: x for x in (offers or []) if x.country_code in order}

        def eligible(code):
            if code in self._no_numbers:
                reasons[code] = "no_numbers"
                return False
            if code in self._failure_blocked:
                reasons[code] = "failure_threshold"
                return False
            if offers is None:
                if allow_order_fallback:
                    return True
                reasons[code] = "no_price_data"
                return False
            item = offer_map.get(code)
            if item is None:
                reasons[code] = "no_quote"
                return False
            if item.available_count <= 0:
                reasons[code] = "no_stock"
                return False
            if self.max_price is not None and item.price > self.max_price:
                reasons[code] = f"over_max_price:{item.price}"
                return False
            return True

        current = self.current_country
        if current and 0 < self.failure_count(current) < self.failure_switch and eligible(current):
            self.last_reason = "same_country_second_attempt"
            self.needs_offer_refresh = False
            return current

        candidates = [code for code in self.preferred_countries if eligible(code)]
        if not candidates and self._failure_blocked:
            for code in list(self._failure_blocked):
                self._failures[code] = 0
            self._failure_blocked.clear()
            reasons.clear()
            candidates = [code for code in self.preferred_countries if eligible(code)]
        if not candidates:
            raise NoEligibleSmsCountry(reasons or {"preferred": "empty"})

        if offers is None:
            chosen = min(candidates, key=lambda code: order[code])
            self.last_reason = "saved_order_fallback"
        else:
            chosen = min(candidates, key=lambda code: (offer_map[code].price, order[code]))
            self.last_reason = "lowest_price"
        self.current_country = chosen
        self.needs_offer_refresh = False
        return chosen

    def record_number_failure(self, country: str) -> int:
        country = str(country)
        count = self._failures.get(country, 0) + 1
        self._failures[country] = count
        if count >= self.failure_switch:
            self._failure_blocked.add(country)
            if self.current_country == country:
                self.current_country = None
            self.needs_offer_refresh = True
        return count

    def record_no_numbers(self, country: str) -> None:
        country = str(country)
        self._no_numbers.add(country)
        if self.current_country == country:
            self.current_country = None
        self.needs_offer_refresh = True

    def failure_count(self, country: str) -> int:
        return self._failures.get(str(country), 0)
```

When `offers is None` and `allow_order_fallback=True`, choose the first saved, non-blocked country. This is the only no-price fallback; never invent a price.

- [ ] **Step 4: Run pure routing tests and verify they pass**

```powershell
python -m pytest tests/test_sms_country_router.py -q
```

Expected: all routing tests pass, including `A,A,B,B,A`.

- [ ] **Step 5: Commit the routing policy**

```powershell
git add core/sms_country_router.py tests/test_sms_country_router.py
git commit -m "feat: route sms attempts across preferred countries"
```

---

### Task 4: Integrate Routing into Roxy Codex Phone Verification

**Files:**
- Modify: `core/roxy_codex_oauth.py:1179-1267`
- Create: `tests/test_roxy_sms_country_routing.py`

- [ ] **Step 1: Write failing Roxy integration tests**

Create `tests/test_roxy_sms_country_routing.py`. Keep Selenium out of the tests by patching the page helpers:

```python
import unittest
from unittest.mock import Mock, patch

from core import roxy_codex_oauth, sms_provider


class RoxySmsCountryRoutingTests(unittest.TestCase):
    def phone_page_patches(self):
        return (
            patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True),
            patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=False),
        )

    def test_no_balance_stops_after_one_acquire_call(self):
        p1, p2 = self.phone_page_patches()
        with p1, p2, patch.object(sms_provider, "_http", return_value=Mock()), patch.object(
            sms_provider, "acquire_number", side_effect=sms_provider.SmsNoBalanceError("NO_BALANCE")
        ) as acquire, patch.object(
            roxy_codex_oauth, "_build_sms_country_selector"
        ) as build:
            build.return_value.choose.return_value = "33"
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                roxy_codex_oauth._do_phone_verification_if_present(Mock())
        acquire.assert_called_once()

    def test_no_numbers_selects_another_country_without_consuming_attempt(self):
        # Patch the first acquire to raise SmsNoNumbersError and the second to return act-2/phone.
        # Patch the later page/SMS helpers to complete successfully.
        # Assert acquire country calls are ["33", "187"] and the log attempt remains 1/5 for the acquired number.
```

For the second test, patch `_ensure_add_phone_input`, `_prepare_phone_submission`, `_click_add_phone_continue_button`, `_wait_page_settle_after_submit`, `_wait_after_phone_send`, `_type_otp`, `_click_if_present`, `_wait_after_phone_otp_submit`, and `human_delay` to return successful minimal values. Use a fake selector whose `record_no_numbers` changes `choose` from `33` to `187`.

- [ ] **Step 2: Run the integration tests and verify they fail**

```powershell
python -m pytest tests/test_roxy_sms_country_routing.py -q
```

Expected: failures because `_build_sms_country_selector` is missing and no-balance is currently caught as a retryable generic exception.

- [ ] **Step 3: Build one selector from hot-loaded configuration**

In `core/roxy_codex_oauth.py`, import the selector and add:

```python
from core.sms_country_router import PreferredCountrySelector, NoEligibleSmsCountry


def _build_sms_country_selector() -> PreferredCountrySelector | None:
    provider = sms_provider._provider()
    if provider not in {"grizzly", "sms_activate"}:
        return None
    return PreferredCountrySelector(
        list(getattr(sms_provider._cfg, "SMS_PREFERRED_COUNTRIES", []) or []),
        fallback_country=str(getattr(sms_provider._cfg, "SMS_COUNTRY", "") or ""),
        failure_switch=int(getattr(sms_provider._cfg, "SMS_COUNTRY_FAILURE_SWITCH", 2) or 2),
        max_price=str(getattr(sms_provider._cfg, "SMS_MAX_PRICE", "") or ""),
    )
```

Add a helper that refreshes live offers and falls back only when the price request itself fails:

```python
def _choose_sms_country(selector, http, *, force=False) -> str | None:
    if selector is None:
        return None
    try:
        offers = sms_provider.get_country_offers(
            selector.preferred_countries,
            service=sms_provider._cfg.SMS_SERVICE,
            http=http,
            force=force,
        )
        return selector.choose(offers)
    except sms_provider.SmsProviderError as exc:
        logger.warning("[Codex][Browser] 国家价格查询失败，按保存顺序回退：%s", exc)
        return selector.choose(None, allow_order_fallback=True)
```

- [ ] **Step 4: Refactor the phone loop to count actual acquired numbers**

Replace the `for attempt in range(...)` loop with an acquired-number loop:

```python
selector = _build_sms_country_selector()
actual_attempt = 0
last_err = None
while actual_attempt < max_retries:
    activation_id = None
    try:
        country = _choose_sms_country(selector, http, force=bool(selector and selector.needs_offer_refresh))
        activation_id, phone = sms_provider.acquire_number(http, country=country)
        actual_attempt += 1
        logger.info(
            "[Codex][Browser] 手机验证尝试 %s/%s，provider=%s，country=%s，号码=+%s",
            actual_attempt, max_retries, provider, country or "fixed", phone,
        )
        # Keep the existing page submission, SMS polling, OTP submission, complete(), and return body.
    except sms_provider.SmsNoBalanceError:
        raise
    except sms_provider.SmsNoNumbersError as exc:
        last_err = exc
        if selector is None or country is None:
            raise
        selector.record_no_numbers(country)
        logger.warning("[Codex][Browser] country=%s 暂无号码，立即切换且不占尝试次数", country)
        continue
    except Exception as exc:
        last_err = exc
        if activation_id and selector is not None and country is not None:
            failures = selector.record_number_failure(country)
            logger.warning("[Codex][Browser] country=%s 连续失败=%s", country, failures)
        # Keep existing cancel, invalid_auth_step guard, page recovery, and 3-8 second delay.
```

Use `actual_attempt` in log labels and the final failure message. Force a fresh offer query after a country reaches the failure threshold by passing `force=True` on the next `_choose_sms_country` call; expose a selector property such as `needs_offer_refresh` rather than reading private fields.

Before each `acquire_number` call, log a compact offer summary (`country`, `price`, `available_count`) and the selector's reason (`lowest_price`, `same_country_second_attempt`, or `saved_order_fallback`). Do not include API credentials. Add the selected-country reason to the selector's public state, for example `last_reason`.

- [ ] **Step 5: Run Roxy and routing tests**

```powershell
python -m pytest tests/test_roxy_sms_country_routing.py tests/test_sms_country_router.py tests/test_sms_provider_sms_activate.py -q
```

Expected: all tests pass; no-balance calls acquire once; no-number switches country without spending an actual attempt.

- [ ] **Step 6: Commit Roxy integration**

```powershell
git add core/roxy_codex_oauth.py tests/test_roxy_sms_country_routing.py
git commit -m "feat: switch sms country after repeated failures"
```

---

### Task 5: Country Catalog API and Searchable WebUI Multi-Select

**Files:**
- Modify: `webui/app.py:2412-2431`
- Modify: `webui/templates/index.html:2941-2962,2981-2989,3210-3240,3345-3367`
- Create: `tests/test_webui_sms_country_options.py`
- Create: `tests/test_webui_sms_country_template.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/test_webui_sms_country_options.py`:

```python
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiSmsCountryOptionsTests(unittest.TestCase):
    def setUp(self):
        with patch("webui.app.db.recover_interrupted_extract_links", return_value={
            "failed_count": 0, "kakao_batches": [],
        }), patch(
            "webui.app.extract_link_service.resume_interrupted_kakao_batches",
            return_value={"resumed_batches": 0, "failed_batches": 0}, create=True,
        ):
            self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("core.sms_provider.list_country_catalog")
    def test_country_catalog_returns_safe_normalized_options(self, catalog):
        catalog.return_value = [
            {"code": "33", "name": "Colombia"},
            {"code": "187", "name": "United States"},
        ]
        response = self.client.get("/api/sms/countries")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "ok": True,
            "countries": [
                {"code": "33", "name": "Colombia"},
                {"code": "187", "name": "United States"},
            ],
        })

    @patch("core.sms_provider.list_country_catalog", side_effect=RuntimeError("provider down"))
    def test_country_catalog_reports_provider_error_without_secrets(self, catalog):
        response = self.client.get("/api/sms/countries")
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["ok"])
        self.assertNotIn("api_key", response.get_data(as_text=True).lower())
```

- [ ] **Step 2: Write failing template contract tests**

Create `tests/test_webui_sms_country_template.py`:

```python
import unittest
from pathlib import Path


class WebUiSmsCountryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

    def test_preferred_country_field_uses_searchable_multiselect(self):
        self.assertIn("renderSmsPreferredCountriesField", self.html)
        self.assertIn('id="smsCountrySearch"', self.html)
        self.assertIn('id="smsCountryOptions"', self.html)
        self.assertIn('data-key="SMS_PREFERRED_COUNTRIES"', self.html)

    def test_country_catalog_is_loaded_from_backend(self):
        self.assertIn("/api/sms/countries", self.html)
        self.assertIn("SMS_COUNTRY_CATALOG", self.html)

    def test_sms_section_contains_new_routing_keys(self):
        self.assertIn("SMS_PREFERRED_COUNTRIES", self.html)
        self.assertIn("SMS_COUNTRY_FAILURE_SWITCH", self.html)
```

- [ ] **Step 3: Run the new tests and verify they fail**

```powershell
python -m pytest tests/test_webui_sms_country_options.py tests/test_webui_sms_country_template.py -q
```

Expected: 404 for the API and missing frontend functions/IDs.

- [ ] **Step 4: Add the authenticated country-catalog route**

In `webui/app.py`, near the Roxy/config helper routes, add:

```python
@app.get("/api/sms/countries")
def api_sms_countries():
    try:
        from core import sms_provider
        countries = sms_provider.list_country_catalog(force=False)
        safe = [
            {"code": str(x.get("code") or ""), "name": str(x.get("name") or x.get("code") or "")}
            for x in countries
            if str(x.get("code") or "").strip()
        ]
        return jsonify({"ok": True, "countries": safe})
    except Exception as exc:
        logger.warning("获取接码国家目录失败：%s: %s", type(exc).__name__, exc)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
```

The existing WebUI auth middleware protects this route; do not return provider configuration or API keys.

- [ ] **Step 5: Render the searchable multi-select**

In `webui/templates/index.html`, add state:

```javascript
let SMS_COUNTRY_CATALOG = [];
let SMS_COUNTRY_CATALOG_ERROR = '';
let SMS_COUNTRY_CATALOG_LOADING = false;
```

Special-case the field at the top of `renderConfigField`:

```javascript
if (f.key === 'SMS_PREFERRED_COUNTRIES') return renderSmsPreferredCountriesField(f, fv);
```

Implement the control with a hidden newline-backed input so the existing save path remains unchanged:

```javascript
function smsPreferredCountryValues(fv) {
  const rows = Array.isArray(fv) ? fv : String(fv || '').split(/\r?\n/);
  return [...new Set(rows.map(x => String(x || '').trim()).filter(Boolean))];
}

function renderSmsPreferredCountriesField(f, fv) {
  const selected = smsPreferredCountryValues(fv);
  const known = new Map(SMS_COUNTRY_CATALOG.map(x => [String(x.code), x]));
  const chips = selected.map(code => {
    const row = known.get(code);
    return `<button type="button" class="sms-country-chip" data-remove-sms-country="${attrEsc(code)}">${esc(row ? row.name : code)} <span>${esc(code)}</span> ×</button>`;
  }).join('');
  return `<label class="fld">${esc(f.label)}<span class="hint">${esc(f.help)} · <span class="mono">${esc(f.key)}</span></span>
    <textarea hidden data-key="SMS_PREFERRED_COUNTRIES">${attrEsc(selected.join('\n'))}</textarea>
    <input id="smsCountrySearch" type="search" placeholder="搜索国家名称或代码" autocomplete="off">
    <div id="smsCountrySelected" class="sms-country-selected">${chips || '<span class="muted">未选择时回退到国家代码</span>'}</div>
    <div id="smsCountryOptions" class="sms-country-options"></div>
    <div id="smsCountryCatalogStatus" class="hint">${SMS_COUNTRY_CATALOG_ERROR ? esc(SMS_COUNTRY_CATALOG_ERROR) : '加载国家目录后可勾选'}</div>
  </label>`;
}
```

Implement catalog loading, filtering, selection order, and hidden-textarea synchronization:

```javascript
function currentSmsCountryCodes() {
  if (Object.prototype.hasOwnProperty.call(CONFIG_PENDING_UPDATES, 'SMS_PREFERRED_COUNTRIES')) {
    return smsPreferredCountryValues(CONFIG_PENDING_UPDATES.SMS_PREFERRED_COUNTRIES);
  }
  const field = CONFIG.find(x => x.key === 'SMS_PREFERRED_COUNTRIES');
  return smsPreferredCountryValues(field ? field.value : []);
}

function renderSmsCountryOptions(query = '') {
  const box = $('#smsCountryOptions');
  if (!box) return;
  const selected = new Set(currentSmsCountryCodes());
  const needle = String(query || '').trim().toLowerCase();
  const rows = SMS_COUNTRY_CATALOG.filter(x => {
    const haystack = `${x.name || ''} ${x.code || ''}`.toLowerCase();
    return !needle || haystack.includes(needle);
  });
  box.innerHTML = rows.map(x => `
    <label class="sms-country-option">
      <input type="checkbox" data-sms-country-code="${attrEsc(x.code)}"${selected.has(String(x.code)) ? ' checked' : ''}>
      <span>${esc(x.name || x.code)}</span><span class="mono">${esc(x.code)}</span>
    </label>`).join('') || '<span class="muted">没有匹配国家</span>';
}

function updateSmsPreferredCountries(codes) {
  const normalized = smsPreferredCountryValues(codes);
  CONFIG_PENDING_UPDATES.SMS_PREFERRED_COUNTRIES = normalized;
  const hidden = $('#tab-config [data-key="SMS_PREFERRED_COUNTRIES"]');
  if (hidden) hidden.value = normalized.join('\n');
  renderConfigPanel();
}

async function loadSmsCountryCatalog() {
  if (SMS_COUNTRY_CATALOG_LOADING || SMS_COUNTRY_CATALOG.length) return;
  SMS_COUNTRY_CATALOG_LOADING = true;
  SMS_COUNTRY_CATALOG_ERROR = '';
  try {
    const result = await api('/api/sms/countries');
    SMS_COUNTRY_CATALOG = Array.isArray(result.countries) ? result.countries : [];
  } catch (error) {
    SMS_COUNTRY_CATALOG_ERROR = `国家目录加载失败：${error.message}`;
  } finally {
    SMS_COUNTRY_CATALOG_LOADING = false;
    if (CONFIG_ACTIVE_GROUP === '接码平台') renderConfigPanel();
  }
}

function bindSmsCountryPicker() {
  const search = $('#smsCountrySearch');
  if (!search) return;
  renderSmsCountryOptions('');
  search.addEventListener('input', () => renderSmsCountryOptions(search.value));
  const options = $('#smsCountryOptions');
  if (options) options.addEventListener('change', event => {
    const checkbox = event.target.closest('[data-sms-country-code]');
    if (!checkbox) return;
    const code = checkbox.dataset.smsCountryCode;
    const selected = currentSmsCountryCodes();
    updateSmsPreferredCountries(
      checkbox.checked ? [...selected, code] : selected.filter(x => x !== code)
    );
  });
  if (!SMS_COUNTRY_CATALOG.length && !SMS_COUNTRY_CATALOG_LOADING) loadSmsCountryCatalog();
}
```

At the beginning of the existing `#configForm` click handler, add chip removal:

```javascript
const removeCountry = e.target.closest('[data-remove-sms-country]');
if (removeCountry) {
  updateSmsPreferredCountries(
    currentSmsCountryCodes().filter(x => x !== removeCountry.dataset.removeSmsCountry)
  );
  return;
}
```

Call `bindSmsCountryPicker()` at the end of `renderConfigPanel()` after `bindRoxyWorkspaceTools()`. Preserve saved unknown codes because chips are built from selected codes even when the catalog is empty or a code is absent.

Add compact styles alongside the existing configuration styles:

```css
.sms-country-selected{display:flex;flex-wrap:wrap;gap:6px;margin:7px 0}
.sms-country-chip{min-height:28px;padding:4px 8px;border-radius:999px;background:var(--soft);border:1px solid var(--line)}
.sms-country-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:6px;max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px}
.sms-country-option{display:flex;align-items:center;gap:7px;padding:6px;border-radius:6px}
.sms-country-option:hover{background:var(--soft)}
.sms-country-option .mono{margin-left:auto;color:var(--muted)}
```

Each selection change updates `CONFIG_PENDING_UPDATES.SMS_PREFERRED_COUNTRIES` with an ordered array and does not save until the user clicks the existing “保存配置” button.

Call `loadSmsCountryCatalog()` after rendering the SMS “通用接码” section when the catalog is still empty; avoid repeated requests while one is in flight. Add compact CSS for `.sms-country-selected`, `.sms-country-chip`, and `.sms-country-options` using the existing color variables.

Update `smsConfigSectionForKey`:

```javascript
if (['SMS_PROVIDER','SMS_COUNTRY','SMS_PREFERRED_COUNTRIES','SMS_COUNTRY_FAILURE_SWITCH','SMS_SERVICE','SMS_MAX_RETRIES','SMS_CODE_WAIT'].includes(key)) {
```

- [ ] **Step 6: Run API/template tests and existing config tests**

```powershell
python -m pytest tests/test_webui_sms_country_options.py tests/test_webui_sms_country_template.py tests/test_config_defaults.py tests/test_sms_provider_sms_activate.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the WebUI slice**

```powershell
git add webui/app.py webui/templates/index.html tests/test_webui_sms_country_options.py tests/test_webui_sms_country_template.py
git commit -m "feat: add sms preferred country picker"
```

---

### Task 6: Documentation, Runtime Default, and Full Verification

**Files:**
- Modify: `README.md:550-580,690-705`
- Runtime-only update: `.env` through `webui.config_editor.update_config` (do not stage `.env`)

- [ ] **Step 1: Document the behavior**

Add a concise section to `README.md`:

```markdown
### HeroSMS 优选国家最低价取号

在 WebUI「配置 → 接码平台 → 通用接码」中选择多个优选国家。HeroSMS/SMS-Activate 会在这些国家中按实时价格选择有库存且不超过 `SMS_MAX_PRICE` 的最低价号码。同一国家连续失败 `SMS_COUNTRY_FAILURE_SWITCH` 次后切换国家；默认总共最多取得 `SMS_MAX_RETRIES=5` 个实际号码。`NO_NUMBERS` 会立即切换且不占实际号码次数，`NO_BALANCE` 会立即停止。
```

Document the new keys in the configuration table.

- [ ] **Step 2: Run focused tests for every changed subsystem**

```powershell
python -m pytest tests/test_config_defaults.py tests/test_sms_provider_sms_activate.py tests/test_sms_country_router.py tests/test_roxy_sms_country_routing.py tests/test_webui_sms_country_options.py tests/test_webui_sms_country_template.py -q
```

Expected: all focused tests pass with zero failures.

- [ ] **Step 3: Run the complete regression suite**

```powershell
python -m pytest -q
```

Expected: the complete suite passes with zero failures.

- [ ] **Step 4: Apply the current worktree runtime default of five**

Run from the worktree without printing secret fields:

```powershell
python -c "from webui import config_editor; print(config_editor.update_config({'SMS_MAX_RETRIES': 5}))"
```

Then verify effective safe settings:

```powershell
python -c "import config; config.reload_all(); from config import codex as c; print({'SMS_MAX_RETRIES': c.SMS_MAX_RETRIES, 'SMS_COUNTRY_FAILURE_SWITCH': c.SMS_COUNTRY_FAILURE_SWITCH, 'SMS_PREFERRED_COUNTRIES': c.SMS_PREFERRED_COUNTRIES})"
```

Expected: `SMS_MAX_RETRIES` is `5`, failure switch is `2`, and the preferred list is either the saved selection or empty with legacy fallback available.

- [ ] **Step 5: Reload and manually verify WebUI**

With `http://127.0.0.1:5002/` running:

1. Reload the page after code changes.
2. Open “配置 → 接码平台 → 通用接码”.
3. Confirm the country catalog loads and search narrows the list.
4. Select at least two countries, save, reload, and confirm selections persist.
5. Confirm “换号重试次数” displays `5` and “连续失败换国家” displays `2`.
6. Restore the user's intended preferred-country selection if temporary QA values were used.

- [ ] **Step 6: Check repository state and preserve logs**

```powershell
git diff --check
git status --short --branch
```

Expected: only intended tracked changes are present; `logs/` remains untracked and untouched.

- [ ] **Step 7: Commit documentation**

```powershell
git add README.md
git commit -m "docs: explain preferred sms country routing"
```

- [ ] **Step 8: Request code review before branch completion**

Use `superpowers:requesting-code-review`, address verified findings only, rerun the focused and complete test suites, and then use `superpowers:finishing-a-development-branch`. Do not switch or merge `main`; leave the completed work on `codex/hero-sms-provider` unless the user explicitly chooses another integration action.
