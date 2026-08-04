# SMS Per-Country Lowest Price Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each compatible SMS request use the selected country's current lowest quote while enforcing the global `$0.11` ceiling.

**Architecture:** Keep `SMS_MAX_PRICE` as the hard ceiling, return the selected `SmsCountryOffer` from the router, and pass that offer's `price` into `acquire_number` as the request-specific `maxPrice`. A missing live quote stops acquisition instead of falling back to a price-blind country request.

**Tech Stack:** Python 3.13, `Decimal`, pytest, unittest mocks, SMS-Activate-compatible HTTP API.

---

## File map

- `core/sms_provider.py`: request-specific `maxPrice` support.
- `core/sms_country_router.py`: complete-offer selection with country-code compatibility.
- `core/roxy_codex_oauth.py`: quote propagation into `getNumber`.
- `tests/test_sms_provider_sms_activate.py`: provider request serialization.
- `tests/test_sms_country_router.py`: selection and refresh behavior.
- `tests/test_roxy_sms_country_routing.py`: end-to-end mocked routing behavior.

### Task 1: Request-specific price cap

**Files:**
- Modify: `tests/test_sms_provider_sms_activate.py`
- Modify: `core/sms_provider.py:554-650`

- [ ] **Step 1: Write the failing tests**

Add to `SmsActivateProviderTests`:

```python
    def test_sms_activate_acquire_uses_per_request_max_price(self):
        http = _Http(["ACCESS_NUMBER:act-low:573001112233"])
        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
        ), patch.object(codex_config, "SMS_API_KEY", "secret"), patch.object(
            codex_config, "SMS_SERVICE", "dr"
        ), patch.object(codex_config, "SMS_COUNTRY", "33"), patch.object(
            codex_config, "SMS_MAX_PRICE", "0.11"
        ):
            result = sms_provider.acquire_number(
                http=http, country="33", max_price="0.055"
            )

        self.assertEqual(result, ("act-low", "573001112233"))
        self.assertEqual(http.calls[0]["params"]["maxPrice"], "0.055")

    def test_sms_activate_acquire_preserves_decimal_max_price(self):
        from decimal import Decimal

        http = _Http(["ACCESS_NUMBER:act-low:573001112233"])
        with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), patch.object(
            codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
        ), patch.object(codex_config, "SMS_API_KEY", "secret"), patch.object(
            codex_config, "SMS_SERVICE", "dr"
        ), patch.object(codex_config, "SMS_COUNTRY", "33"), patch.object(
            codex_config, "SMS_MAX_PRICE", "0.11"
        ):
            sms_provider.acquire_number(
                http=http, country="33", max_price=Decimal("0.0275")
            )

        self.assertEqual(http.calls[0]["params"]["maxPrice"], "0.0275")
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py::SmsActivateProviderTests::test_sms_activate_acquire_uses_per_request_max_price tests/test_sms_provider_sms_activate.py::SmsActivateProviderTests::test_sms_activate_acquire_preserves_decimal_max_price -q
```

Expected: `TypeError` because `acquire_number` has no `max_price` keyword.

- [ ] **Step 3: Implement the minimal provider change**

Use this signature in `core/sms_provider.py`:

```python
def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
    *,
    max_price=None,
) -> tuple[str, str]:
```

Build the compatible-provider request price with:

```python
        request_max_price = (
            str(max_price).strip()
            if max_price is not None
            else str(_cfg.SMS_MAX_PRICE or "").strip()
        )
        if request_max_price:
            params["maxPrice"] = request_max_price
```

Leave the `l` and `h` branches unchanged. A caller without an override must still use `SMS_MAX_PRICE`.

- [ ] **Step 4: Verify GREEN**

Run `python -m pytest tests/test_sms_provider_sms_activate.py -q`.

Expected: the complete file passes.

- [ ] **Step 5: Commit**

```powershell
git add core/sms_provider.py tests/test_sms_provider_sms_activate.py
git commit -m "fix: support per-request SMS price caps"
```

### Task 2: Return the selected live offer

**Files:**
- Modify: `tests/test_sms_country_router.py`
- Modify: `core/sms_country_router.py`

- [ ] **Step 1: Write failing router tests**

```python
def test_choose_offer_returns_lowest_complete_offer():
    selector = PreferredCountrySelector(
        ["31", "33"], fallback_country="33", max_price="0.11"
    )
    selected = selector.choose_offer(
        [offer("31", "0.055", 3), offer("33", "0.0275", 9)]
    )

    assert selected == offer("33", "0.0275", 9)
    assert selector.current_country == "33"
    assert selector.last_reason == "lowest_price"


def test_choose_country_compatibility_wraps_choose_offer():
    selector = PreferredCountrySelector(
        ["31", "33"], fallback_country="33", max_price="0.11"
    )
    assert selector.choose(
        [offer("31", "0.055", 3), offer("33", "0.0275", 9)]
    ) == "33"


def test_number_failure_requires_fresh_same_country_quote():
    selector = PreferredCountrySelector(
        ["31", "33"], fallback_country="33", failure_switch=2, max_price="0.11"
    )
    assert selector.choose_offer(
        [offer("31", "0.055"), offer("33", "0.08")]
    ).country_code == "31"
    selector.record_number_failure("31")

    assert selector.needs_offer_refresh is True
    assert selector.choose_offer(
        [offer("31", "0.04"), offer("33", "0.05")]
    ) == offer("31", "0.04")
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m pytest tests/test_sms_country_router.py::test_choose_offer_returns_lowest_complete_offer tests/test_sms_country_router.py::test_choose_country_compatibility_wraps_choose_offer tests/test_sms_country_router.py::test_number_failure_requires_fresh_same_country_quote -q
```

Expected: `choose_offer` is missing and the first failure does not request a refresh.

- [ ] **Step 3: Implement `choose_offer`**

Move live-offer selection into:

```python
    def choose_offer(
        self,
        offers: list[SmsCountryOffer] | None,
    ) -> SmsCountryOffer:
```

For the normal lowest-price branch:

```python
        code = min(
            eligible,
            key=lambda candidate: (
                eligible[candidate].price,
                preference_index[candidate],
            ),
        )
        self._select(code, "lowest_price")
        return eligible[code]
```

For the same-country second attempt, return `eligible[self.current_country]` after `_select`. `choose_offer(None)` must raise `NoEligibleSmsCountry` because a safe price is unavailable.

Keep compatibility:

```python
    def choose(self, offers, *, allow_order_fallback=False) -> str:
        if offers is None and allow_order_fallback:
            return self._choose_country_without_offers()
        return self.choose_offer(offers).country_code
```

Extract the existing saved-order fallback body into `_choose_country_without_offers`. In `record_number_failure`, set `self.needs_offer_refresh = True` for both the first failure and threshold failure.

- [ ] **Step 4: Verify GREEN**

Run `python -m pytest tests/test_sms_country_router.py -q`.

Expected: all router tests pass, including existing fallback compatibility tests.

- [ ] **Step 5: Commit**

```powershell
git add core/sms_country_router.py tests/test_sms_country_router.py
git commit -m "feat: preserve selected SMS country quotes"
```

### Task 3: Propagate the quote through the Roxy flow

**Files:**
- Modify: `tests/test_roxy_sms_country_routing.py`
- Modify: `core/roxy_codex_oauth.py:1205-1284`

- [ ] **Step 1: Write the Colombia regression test**

```python
def test_colombia_lowest_quote_becomes_get_number_max_price():
    provider_cfg = cfg()
    provider_cfg.SMS_PREFERRED_COUNTRIES = ["33"]
    provider_cfg.SMS_COUNTRY = "33"
    provider_cfg.SMS_MAX_PRICE = "0.11"

    acquire, _, _ = run_phone_flow(
        provider_cfg,
        offers=[offer("33", "0.055", 4857)],
        acquire=[("id-co", "573001112233")],
    )

    acquire.assert_called_once_with(
        ANY, country="33", max_price=Decimal("0.055")
    )
```

Update the no-number switching assertion:

```python
    assert acquire.call_args_list == [
        call(ANY, country="A", max_price=Decimal("0.10")),
        call(ANY, country="B", max_price=Decimal("0.20")),
    ]
```

Add a price-transport safety test:

```python
def test_price_transport_error_stops_before_number_acquisition():
    provider_cfg = cfg()
    http = Mock()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(roxy_codex_oauth.sms_provider, "_cfg", provider_cfg)
        )
        stack.enter_context(
            patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http)
        )
        stack.enter_context(
            patch.object(
                roxy_codex_oauth.sms_provider,
                "get_country_offers",
                side_effect=SmsProviderError("prices unavailable"),
            )
        )
        acquire = stack.enter_context(
            patch.object(roxy_codex_oauth.sms_provider, "acquire_number")
        )
        stack.enter_context(
            patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True)
        )
        with pytest.raises(SmsProviderError, match="prices unavailable"):
            roxy_codex_oauth._do_phone_verification_if_present(object())

    acquire.assert_not_called()
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_roxy_sms_country_routing.py -q`.

Expected: compatible acquisitions lack `max_price`, and price API failure still uses saved-order fallback.

- [ ] **Step 3: Implement `_choose_sms_offer`**

Replace the compatible Roxy helper with:

```python
def _choose_sms_offer(selector, http, force: bool = False):
    if selector is None:
        return None
    offers = sms_provider.get_country_offers(
        selector.preferred_countries,
        service=sms_provider._cfg.SMS_SERVICE,
        http=http,
        force=force,
    )
    summary = ",".join(
        f"{item.country_code}={item.price}/{item.available_count}"
        for item in offers
    ) or "-"
    selected = None
    try:
        selected = selector.choose_offer(offers)
        return selected
    finally:
        logger.info(
            "[Codex][Browser] SMS 国家选择 country=%s reason=%s offers=%s quoted_price=%s request_max_price=%s stock=%s",
            selected.country_code if selected else "-",
            selector.last_reason,
            summary,
            selected.price if selected else "-",
            selected.price if selected else "-",
            selected.available_count if selected else "-",
        )
```

Do not catch general quote errors. They must stop before number acquisition.

- [ ] **Step 4: Pass the quote into `acquire_number`**

Inside the phone loop:

```python
            selected_offer = _choose_sms_offer(
                selector,
                http,
                force=bool(selector and selector.needs_offer_refresh),
            )
            country = selected_offer.country_code if selected_offer else None
            request_max_price = selected_offer.price if selected_offer else None
```

Use separate compatible and fixed-provider calls:

```python
                if selected_offer is None:
                    activation_id, phone = sms_provider.acquire_number(
                        http, country=country
                    )
                else:
                    activation_id, phone = sms_provider.acquire_number(
                        http,
                        country=country,
                        max_price=request_max_price,
                    )
```

Log `country` and `request_max_price`; never log the API key.

- [ ] **Step 5: Verify GREEN**

Run `python -m pytest tests/test_roxy_sms_country_routing.py -q`.

Expected: all tests pass using mocks; no HeroSMS order is created.

- [ ] **Step 6: Commit**

```powershell
git add core/roxy_codex_oauth.py tests/test_roxy_sms_country_routing.py
git commit -m "fix: acquire SMS numbers at selected country quote"
```

### Task 4: Full verification

**Files:**
- No planned production changes.

- [ ] **Step 1: Run focused tests**

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py tests/test_sms_country_router.py tests/test_roxy_sms_country_routing.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run syntax validation**

```powershell
python -m py_compile core/sms_provider.py core/sms_country_router.py core/roxy_codex_oauth.py
```

Expected: exit code `0`, no output.

- [ ] **Step 3: Run the complete suite**

```powershell
python -m pytest -q
```

Expected: every test passes; tests use fake HTTP responses and mocks only.

- [ ] **Step 4: Inspect the final diff**

```powershell
git diff HEAD~3 --check
git status --short --branch
```

Expected: no whitespace errors and only the known untracked `logs/` directory remains.

- [ ] **Step 5: Report evidence**

Report the focused and complete test totals, commit hashes, branch, confirmation of zero real SMS orders, and the resulting behavior: a Colombia `$0.055` quote produces `country=33&maxPrice=0.055` even though the global ceiling remains `$0.11`.
