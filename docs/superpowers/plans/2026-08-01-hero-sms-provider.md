# HeroSMS / SMS-Activate Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class HeroSMS/SMS-Activate configuration while reusing the project's existing SMS-Activate-compatible number and OTP protocol.

**Architecture:** Generalize the current Grizzly text-handler branch into a shared compatible-handler path. Preserve the existing public SMS provider functions and L/H backends, while adding provider aliases, environment/WebUI configuration, provider-aware cancellation timing, focused tests, and documentation.

**Tech Stack:** Python 3, `curl_cffi`, `unittest`, Flask WebUI configuration editor, `.env` overrides.

---

### Task 1: Define provider compatibility and configuration contract

**Files:**
- Create: `tests/test_sms_provider_sms_activate.py`
- Modify: `config/codex.py`
- Modify: `webui/config_editor.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing configuration tests**

Create tests that assert `SMS_API_BASE` and `SMS_CANCEL_DELAY` are present in
`webui.config_editor.EDITABLE_FIELDS`, that the API base uses environment
storage, and that `config.codex` exposes the two settings.

```python
def test_sms_activate_config_is_exposed_in_webui(self):
    fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
    self.assertIn("SMS_API_BASE", fields)
    self.assertEqual(fields["SMS_API_BASE"].get("storage"), "env")
    self.assertIn("SMS_CANCEL_DELAY", fields)
    self.assertTrue(hasattr(codex_config, "SMS_API_BASE"))
    self.assertTrue(hasattr(codex_config, "SMS_CANCEL_DELAY"))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py::SmsActivateProviderTests::test_sms_activate_config_is_exposed_in_webui -q
```

Expected: failure because `SMS_CANCEL_DELAY` and the WebUI API-base field are
not defined.

- [ ] **Step 3: Add the minimal configuration fields**

In `config/codex.py`, make the handler base environment-overridable and add the
automatic cancel-delay setting:

```python
SMS_API_BASE: str = env_str(
    "SMS_API_BASE",
    "https://api.grizzlysms.com/stubs/handler_api.php",
)
SMS_CANCEL_DELAY: int = env_int("SMS_CANCEL_DELAY", -1)
```

Add both keys to `apply_env_overrides(...)`. In `webui/config_editor.py`, add an
environment-backed API-base field and an integer cancel-delay field. Add the
same variables and provider choices to `.env.example`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py::SmsActivateProviderTests::test_sms_activate_config_is_exposed_in_webui -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit the configuration contract**

```powershell
git add tests/test_sms_provider_sms_activate.py config/codex.py webui/config_editor.py .env.example
git commit -m "feat: expose sms activate provider config"
```

### Task 2: Generalize the compatible handler transport

**Files:**
- Modify: `tests/test_sms_provider_sms_activate.py`
- Modify: `core/sms_provider.py`

- [ ] **Step 1: Write failing provider and acquisition tests**

Add a fake HTTP client that records GET calls and returns text responses. Test
provider aliases and a full `getNumber` request:

```python
def test_sms_activate_acquire_uses_configured_handler(self):
    http = _Http(["ACCESS_NUMBER:act-1:15551234567"])
    with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), \
         patch.object(codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"), \
         patch.object(codex_config, "SMS_API_KEY", "secret"), \
         patch.object(codex_config, "SMS_SERVICE", "dr"), \
         patch.object(codex_config, "SMS_COUNTRY", "12"), \
         patch.object(codex_config, "SMS_MAX_PRICE", "1.5"):
        activation_id, phone = sms_provider.acquire_number(http=http)

    self.assertEqual((activation_id, phone), ("act-1", "15551234567"))
    self.assertEqual(http.calls[0]["url"], "https://hero-sms.com/stubs/handler_api.php")
    self.assertEqual(http.calls[0]["params"]["action"], "getNumber")
    self.assertEqual(http.calls[0]["params"]["api_key"], "secret")
    self.assertEqual(http.calls[0]["params"]["service"], "dr")
    self.assertEqual(http.calls[0]["params"]["country"], "12")
    self.assertEqual(http.calls[0]["params"]["maxPrice"], "1.5")
```

Also assert `sms-activate`, `smsactivate`, `hero_sms`, and `hero-sms` normalize
to `sms_activate`.

- [ ] **Step 2: Run the acquisition tests and verify RED**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py -q
```

Expected: failures because provider normalization and the generic handler
transport are not implemented.

- [ ] **Step 3: Implement the shared compatible-handler path**

In `core/sms_provider.py`:

```python
_HANDLER_PROVIDERS = {"grizzly", "sms_activate"}

def _provider() -> str:
    raw = str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()
    normalized = raw.replace("-", "_")
    if normalized in {"smsactivate", "hero_sms"}:
        return "sms_activate"
    return normalized

def _request_handler(http: CurlSession, params: dict) -> str:
    base_params = {"api_key": _cfg.SMS_API_KEY}
    base_params.update(params)
    resp = http.get(_cfg.SMS_API_BASE, params=base_params)
    # Preserve the current status and text-error parsing.
```

Route `acquire_number`, `wait_for_sms_code`, and `set_status` through
`_request_handler`. Keep L/H branches and public function signatures intact.

- [ ] **Step 4: Run the provider tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py -q
```

Expected: all tests in the new file pass.

- [ ] **Step 5: Run existing SMS tests**

Run:

```powershell
python -m pytest tests/test_sms_provider_h.py tests/test_sms_provider_sms_activate.py -q
```

Expected: all focused SMS tests pass.

- [ ] **Step 6: Commit the shared transport**

```powershell
git add core/sms_provider.py tests/test_sms_provider_sms_activate.py
git commit -m "feat: support sms activate compatible handlers"
```

### Task 3: Add provider-aware cancellation

**Files:**
- Modify: `tests/test_sms_provider_sms_activate.py`
- Modify: `core/sms_provider.py`

- [ ] **Step 1: Write failing lifecycle tests**

Add tests proving `STATUS_OK:<code>` parsing, status `6` completion, and status
`8` cancellation. Patch `time.sleep` and assert that SMS-Activate cancellation
does not wait when `SMS_CANCEL_DELAY=-1`:

```python
def test_sms_activate_cancel_has_no_grizzly_delay(self):
    http = _Http(["ACCESS_CANCEL"])
    sms_provider._ACQUIRED_AT["act-1"] = time.time()
    with patch.object(codex_config, "SMS_PROVIDER", "sms_activate"), \
         patch.object(codex_config, "SMS_CANCEL_DELAY", -1), \
         patch("core.sms_provider.time.sleep") as sleep:
        sms_provider.cancel("act-1", http=http, background=False)

    sleep.assert_not_called()
    self.assertEqual(http.calls[0]["params"]["status"], "8")
```

- [ ] **Step 2: Run the lifecycle tests and verify RED**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py -q
```

Expected: cancellation test fails because the current implementation always
applies the Grizzly 125-second delay.

- [ ] **Step 3: Implement automatic cancellation delay selection**

Add:

```python
def _cancel_delay_seconds() -> int:
    configured = int(getattr(_cfg, "SMS_CANCEL_DELAY", -1) or 0)
    if configured >= 0:
        return configured
    return _MIN_CANCEL_DELAY if _provider() == "grizzly" else 0
```

Use this value in `_do_cancel_sync` and update log text so it names the current
provider rather than always saying GrizzlySMS.

- [ ] **Step 4: Run lifecycle and focused SMS tests**

Run:

```powershell
python -m pytest tests/test_sms_provider_sms_activate.py tests/test_sms_provider_h.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit lifecycle behavior**

```powershell
git add core/sms_provider.py tests/test_sms_provider_sms_activate.py
git commit -m "fix: use provider aware sms cancellation delay"
```

### Task 4: Document HeroSMS setup and verify the complete project

**Files:**
- Modify: `README.md`
- Modify: `config/codex.py`
- Modify: `core/sms_provider.py`
- Modify: `webui/config_editor.py`
- Modify: `.env.example`
- Test: `tests/test_sms_provider_sms_activate.py`

- [ ] **Step 1: Update user documentation**

Document this configuration:

```dotenv
SMS_PROVIDER=sms_activate
SMS_API_BASE=https://hero-sms.com/stubs/handler_api.php
SMS_API_KEY=your_hero_sms_api_key
SMS_SERVICE=dr
SMS_COUNTRY=country_id
SMS_CANCEL_DELAY=-1
```

Explain that `hero_sms` is accepted as an alias, `-1` chooses the provider
default, and the service/country codes must match the HeroSMS catalog.

- [ ] **Step 2: Run the full test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all existing and new tests pass with zero failures.

- [ ] **Step 3: Inspect the final diff and configuration references**

Run:

```powershell
git diff --check
git status --short
rg -n "sms_activate|hero_sms|SMS_API_BASE|SMS_CANCEL_DELAY" core config webui tests README.md .env.example
```

Expected: no whitespace errors; only planned files are modified; every new
configuration key is represented in runtime code, WebUI, tests, examples, and
documentation.

- [ ] **Step 4: Commit documentation and final integration**

```powershell
git add README.md .env.example config/codex.py core/sms_provider.py webui/config_editor.py tests/test_sms_provider_sms_activate.py
git commit -m "docs: add hero sms provider setup"
```

- [ ] **Step 5: Re-run final verification after the commit**

Run:

```powershell
python -m pytest -q
git status --short --branch
```

Expected: the complete suite passes and the worktree is clean on
`codex/hero-sms-provider`.
