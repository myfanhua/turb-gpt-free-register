# Account Registration Location Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the actual proxy exit country/region for newly registered accounts and display the country plus optional IP in the WebUI account table.

**Architecture:** A focused `core.registration_location` module performs one best-effort JSON lookup through the exact registration proxy. `save_account_data()` invokes it centrally so all registration implementations that already pass `proxy_used` gain the feature without duplicating network code; normalized location fields are persisted as optional account fields and exposed only through the compact account-list API.

**Tech Stack:** Python 3.13, `curl_cffi.requests`, JSON file persistence, Flask, native JavaScript, `unittest`/`pytest`.

---

## File Structure

- Create `core/registration_location.py`: validate proxy URLs, query the exit-location endpoint through the proxy, normalize partial responses, and degrade to an empty result.
- Create `tests/test_registration_location.py`: unit coverage for proxy routing, normalization, invalid proxy values, provider errors, and transport failures.
- Modify `core/account_export.py`: perform the best-effort lookup before calling `insert_account()`.
- Modify `core/db.py`: accept and persist four optional registration-location fields while preserving existing values on later updates.
- Modify `tests/test_account_export_plan_proxy.py`: verify location lookup and persistence forwarding without making network requests.
- Modify `webui/app.py`: include optional registration-location fields in compact account rows.
- Modify `tests/test_webui_extract_link_provider.py`: verify compact API exposure and continued proxy credential redaction.
- Modify `webui/templates/index.html`: add the account-table column, localized country rendering, optional IP subline, tooltip, widths, and empty-row colspan.
- Create `tests/test_account_registration_location_ui.py`: static template regression coverage for the new column and rendering contract.

### Task 1: Registration location lookup module

**Files:**
- Create: `core/registration_location.py`
- Create: `tests/test_registration_location.py`

- [ ] **Step 1: Write the failing lookup tests**

```python
import unittest
from unittest.mock import Mock

from core.registration_location import lookup_registration_location


class RegistrationLocationTests(unittest.TestCase):
    def test_lookup_uses_exact_proxy_and_normalizes_fields(self):
        transport = Mock(return_value={
            "ip": "203.0.113.10",
            "country_code": "us",
            "country_name": "United States",
            "region": "California",
        })

        result = lookup_registration_location(
            "http://sid:bridge@127.0.0.1:25001",
            transport=transport,
        )

        transport.assert_called_once_with(
            "https://ipapi.co/json/",
            "http://sid:bridge@127.0.0.1:25001",
            5.0,
        )
        self.assertEqual(result, {
            "country_code": "US",
            "country": "United States",
            "region": "California",
            "ip": "203.0.113.10",
        })

    def test_lookup_returns_empty_for_non_proxy_marker(self):
        transport = Mock()
        self.assertEqual(
            lookup_registration_location("browseruse:us", transport=transport),
            {},
        )
        transport.assert_not_called()

    def test_lookup_keeps_partial_response(self):
        transport = Mock(return_value={"country_code": "NL"})
        self.assertEqual(
            lookup_registration_location(
                "socks5://127.0.0.1:7897",
                transport=transport,
            ),
            {"country_code": "NL", "country": "", "region": "", "ip": ""},
        )

    def test_lookup_failure_does_not_escape(self):
        transport = Mock(side_effect=TimeoutError("slow"))
        self.assertEqual(
            lookup_registration_location(
                "http://127.0.0.1:25001",
                transport=transport,
            ),
            {},
        )
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/test_registration_location.py -q`

Expected: collection fails because `core.registration_location` does not exist.

- [ ] **Step 3: Implement the minimal lookup module**

```python
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
    parsed = urlparse(str(proxy_url or "").strip())
    return (
        parsed.scheme.lower() in _SUPPORTED_PROXY_SCHEMES
        and bool(parsed.hostname)
        and parsed.port is not None
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
        raise RuntimeError(str(payload.get("reason") or payload.get("error") or "invalid response"))
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
            "country_code": str(payload.get("country_code") or payload.get("country") or "").strip().upper(),
            "country": str(payload.get("country_name") or "").strip(),
            "region": str(payload.get("region") or payload.get("region_name") or "").strip(),
            "ip": str(payload.get("ip") or "").strip(),
        }
    except Exception as exc:
        logger.warning("注册出口位置查询失败：%s: %s", type(exc).__name__, str(exc)[:180])
        return {}
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python -m pytest tests/test_registration_location.py -q`

Expected: `4 passed`.

- [ ] **Step 5: Commit the lookup module**

```powershell
git add core/registration_location.py tests/test_registration_location.py
git commit -m "feat: resolve registration proxy location"
```

### Task 2: Persist location with newly saved accounts

**Files:**
- Modify: `tests/test_account_export_plan_proxy.py`
- Modify: `core/account_export.py`
- Modify: `core/db.py`

- [ ] **Step 1: Add failing account export tests**

Patch `lookup_registration_location` in the existing proxy test and assert that `insert_account()` receives:

```python
registration_country_code="US",
registration_country="United States",
registration_region="California",
registration_ip="203.0.113.10",
```

Add a second assertion that a lookup result of `{}` forwards `None` values and does not affect the existing plan-check proxy argument.

- [ ] **Step 2: Run the export tests and verify RED**

Run: `python -m pytest tests/test_account_export_plan_proxy.py -q`

Expected: failure because location lookup is not invoked and `insert_account()` has no location arguments.

- [ ] **Step 3: Forward normalized fields from `save_account_data()`**

Add:

```python
from core.registration_location import lookup_registration_location

registration_location = lookup_registration_location(proxy_used)
```

Then extend the `insert_account()` call:

```python
registration_country_code=registration_location.get("country_code") or None,
registration_country=registration_location.get("country") or None,
registration_region=registration_location.get("region") or None,
registration_ip=registration_location.get("ip") or None,
```

- [ ] **Step 4: Extend `insert_account()` and preserve existing values**

Add four optional keyword parameters and row updates following the existing `proxy_used` pattern:

```python
"registration_country_code": (
    registration_country_code
    if registration_country_code is not None
    else row.get("registration_country_code")
),
"registration_country": (
    registration_country
    if registration_country is not None
    else row.get("registration_country")
),
"registration_region": (
    registration_region
    if registration_region is not None
    else row.get("registration_region")
),
"registration_ip": (
    registration_ip
    if registration_ip is not None
    else row.get("registration_ip")
),
```

- [ ] **Step 5: Run focused persistence tests**

Run: `python -m pytest tests/test_account_export_plan_proxy.py tests/test_icloud_pool.py -q`

Expected: all tests pass with no network request because the test patches the resolver.

- [ ] **Step 6: Commit persistence changes**

```powershell
git add core/account_export.py core/db.py tests/test_account_export_plan_proxy.py
git commit -m "feat: persist registration location on accounts"
```

### Task 3: Expose safe location fields in the compact account API

**Files:**
- Modify: `tests/test_webui_extract_link_provider.py`
- Modify: `webui/app.py`

- [ ] **Step 1: Add a failing compact-row test**

Extend the existing compact-account test row with location and proxy fields:

```python
"registration_country_code": "US",
"registration_country": "United States",
"registration_region": "California",
"registration_ip": "203.0.113.10",
"proxy_used": "http://secret:password@127.0.0.1:25001",
```

Assert all four location fields are returned and `proxy_used` is absent.

- [ ] **Step 2: Run the compact-row test and verify RED**

Run: `python -m pytest tests/test_webui_extract_link_provider.py::WebuiExtractLinkProviderTests::test_compact_account_exposes_batch_status_without_secrets -q`

Expected: failure because location fields are absent.

- [ ] **Step 3: Add location fields to compact output**

Append to `optional_keys` in `_compact_account_for_list()`:

```python
"registration_country_code", "registration_country",
"registration_region", "registration_ip",
```

Do not add `proxy_used`.

- [ ] **Step 4: Run focused WebUI API tests**

Run: `python -m pytest tests/test_webui_extract_link_provider.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit API changes**

```powershell
git add webui/app.py tests/test_webui_extract_link_provider.py
git commit -m "feat: expose account registration location"
```

### Task 4: Display country and optional IP in the account table

**Files:**
- Create: `tests/test_account_registration_location_ui.py`
- Modify: `webui/templates/index.html`

- [ ] **Step 1: Add failing template regression tests**

```python
from pathlib import Path
import unittest


class AccountRegistrationLocationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_account_table_has_registration_location_column(self):
        self.assertIn("<th>注册地址</th>", self.html)
        self.assertIn('class="col-location"', self.html)

    def test_account_rows_render_country_and_optional_ip(self):
        self.assertIn("function _registrationLocationCell(r)", self.html)
        self.assertIn("registration_country_code", self.html)
        self.assertIn("registration_region", self.html)
        self.assertIn("registration_ip", self.html)
        self.assertIn("_registrationLocationCell(r)", self.html)

    def test_empty_account_row_spans_new_column_count(self):
        self.assertIn('colspan="13" class="muted">暂无账号', self.html)
```

- [ ] **Step 2: Run the UI tests and verify RED**

Run: `python -m pytest tests/test_account_registration_location_ui.py -q`

Expected: three failures because the column and renderer do not exist.

- [ ] **Step 3: Add the table column and styling**

Add `.col-location { width: 140px; }` and `.accounts-table .col-location { width: 140px; }`, insert `<col class="col-location">` after the source column, and insert `<th>注册地址</th>` after `<th>来源</th>`.

Update account-table `nth-child` selectors by one for every column after the inserted location column, and change the empty-state `colspan` from `12` to `13`.

- [ ] **Step 4: Add localized rendering**

Add a cached `Intl.DisplayNames` helper and the cell renderer:

```javascript
let REGISTRATION_REGION_NAMES = null;
function _registrationCountryName(r) {
  const code = String(r.registration_country_code || '').trim().toUpperCase();
  const fallback = String(r.registration_country || code || '').trim();
  if (!code || typeof Intl === 'undefined' || typeof Intl.DisplayNames !== 'function') return fallback;
  try {
    REGISTRATION_REGION_NAMES ||= new Intl.DisplayNames(['zh-CN'], {type: 'region'});
    return REGISTRATION_REGION_NAMES.of(code) || fallback;
  } catch (_) {
    return fallback;
  }
}

function _registrationLocationCell(r) {
  const country = _registrationCountryName(r);
  const region = String(r.registration_region || '').trim();
  const ip = String(r.registration_ip || '').trim();
  if (!country && !region && !ip) return '<span class="muted">-</span>';
  const title = [country, region, ip].filter(Boolean).join(' · ');
  return `<div title="${esc(title)}"><div class="main-cell">${esc(country || region || '-')}</div>${ip ? `<div class="sub-cell mono">${esc(ip)}</div>` : ''}</div>`;
}
```

Render `${_registrationLocationCell(r)}` after the email-source cell.

- [ ] **Step 5: Run the UI tests and focused WebUI tests**

Run: `python -m pytest tests/test_account_registration_location_ui.py tests/test_webui_extract_link_provider.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit UI changes**

```powershell
git add webui/templates/index.html tests/test_account_registration_location_ui.py
git commit -m "feat: show registration location in account list"
```

### Task 5: Full verification and WebUI restart

**Files:**
- No production file changes expected.

- [ ] **Step 1: Run static diff checks**

Run: `git diff --check`

Expected: exit code 0.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`

Expected: all tests pass, including the new location tests.

- [ ] **Step 3: Confirm no active registration tasks**

Read `注册任务.json` and verify there are no jobs with status `pending`, `running`, `stopping`, or `queued`. If an active task exists, wait for it to finish before restarting.

- [ ] **Step 4: Restart only HeroSMS WebUI port 5002**

Terminate the single process listening on `127.0.0.1:5002`, then start:

```powershell
python web.py --host 127.0.0.1 --port 5002
```

as a hidden process with stdout/stderr redirected to `logs/desktop-webui-5002.out.log` and `logs/desktop-webui-5002.err.log`.

- [ ] **Step 5: Verify the running service**

Confirm:

- `http://127.0.0.1:5002/` returns HTTP 200 after authentication redirect handling.
- Exactly one process listens on port 5002.
- The process command points to the HeroSMS worktree.
- No real registration is started.

- [ ] **Step 6: Record final status**

Run:

```powershell
git status --short --branch
git log -6 --oneline
```

Expected: only ignored/untracked runtime `logs/` remains; feature commits are present on `codex/hero-sms-provider`.
