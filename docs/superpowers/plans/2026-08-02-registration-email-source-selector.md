# Registration Email Source Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-registration-batch email source selector, including strict iCloud API-only and URL-only modes, without mutating global configuration or allowing concurrent batches to affect one another.

**Architecture:** Store the selected source string on every job and make workers acquire email from that job snapshot. Introduce two virtual iCloud selectors (`icloud_api_token`, `icloud_url`) that filter the shared iCloud pool and persist a temporary claimed pickup mode. Keep `icloud_api` as the backward-compatible “iCloud all” behavior and keep registered account source canonicalized to `icloud_api`.

**Tech Stack:** Python 3, Flask, JSON-backed mailbox/job storage, native JavaScript, `unittest`/pytest.

---

### Task 1: Registration source catalog and parameterized email acquisition

**Files:**
- Modify: `core/email_provider.py`
- Create: `tests/test_registration_email_source_selector.py`
- Modify: `tests/test_email_provider_gptmail.py`
- Modify: `tests/test_icloud_email_provider.py`

- [ ] **Step 1: Write failing tests for source metadata and explicit acquisition**

Create `tests/test_registration_email_source_selector.py` with tests equivalent to:

```python
class RegistrationEmailSourceSelectorTests(unittest.TestCase):
    def test_source_options_include_distinct_icloud_modes(self):
        values = [item["value"] for item in email_provider.registration_source_options()]
        self.assertIn("icloud_api", values)
        self.assertIn("icloud_api_token", values)
        self.assertIn("icloud_url", values)

    @patch("core.email_provider._pick_from_source", return_value="one@icloud.com")
    def test_acquire_email_uses_explicit_source_instead_of_global_config(self, pick):
        with patch.object(email_provider._email_cfg, "EMAIL_SOURCE", "outlook"):
            email = email_provider.acquire_email("icloud_url")
        self.assertEqual(email, "one@icloud.com")
        pick.assert_called_once_with("icloud_url")

    def test_canonical_source_maps_virtual_icloud_modes_to_icloud_api(self):
        self.assertEqual(email_provider.canonical_email_source("icloud_api_token"), "icloud_api")
        self.assertEqual(email_provider.canonical_email_source("icloud_url"), "icloud_api")
```

Also assert that an explicit single source does not append configured fallback sources, while `acquire_email()` without an argument retains the configured ordered fallback behavior.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m pytest tests/test_registration_email_source_selector.py tests/test_email_provider_gptmail.py tests/test_icloud_email_provider.py -q
```

Expected: failures because the option catalog, virtual sources, canonical mapping, and `acquire_email(value)` parameter do not exist.

- [ ] **Step 3: Implement the source catalog and virtual aliases**

In `core/email_provider.py`, extend the valid source set and add labels:

```python
_VALID_SOURCES = (
    "outlook",
    "generic_api",
    "cloudflare_domain",
    "cloudflare",
    "gptmail",
    "mailnest",
    "cloudmail",
    "icloud_api",
    "icloud_api_token",
    "icloud_url",
)

_REGISTRATION_SOURCE_OPTIONS = (
    {"value": "icloud_api", "label": "iCloud 全部"},
    {"value": "icloud_api_token", "label": "iCloud API"},
    {"value": "icloud_url", "label": "iCloud 独立 URL"},
    {"value": "outlook", "label": "Outlook"},
    {"value": "generic_api", "label": "通用 API"},
    {"value": "cloudflare_domain", "label": "Cloudflare 域名邮箱"},
    {"value": "cloudflare", "label": "Cloudflare 临时邮箱"},
    {"value": "gptmail", "label": "GPTMail"},
    {"value": "mailnest", "label": "MailNest"},
    {"value": "cloudmail", "label": "CloudMail"},
)
```

Add:

```python
def registration_source_options() -> list[dict]:
    return [dict(item) for item in _REGISTRATION_SOURCE_OPTIONS]

def canonical_email_source(source: str) -> str:
    value = str(source or "").strip()
    return "icloud_api" if value in {"icloud_api_token", "icloud_url"} else value

def snapshot_registration_source(value=None) -> str:
    if value is None or not str(value).strip():
        from config import email as _email_cfg
        value = _email_cfg.EMAIL_SOURCE
    return ",".join(parse_email_sources(value))
```

Change `acquire_email()` to accept an optional value:

```python
def acquire_email(value=None) -> str:
    sources = parse_email_sources(value)
    ...
```

Update `_pick_from_source()` so `icloud_api_token` and `icloud_url` call `icloud_mail_client.pick_account(selection="token")` and `pick_account(selection="url")`. Keep `icloud_api` mapped to `selection="all"`.

Use `canonical_email_source()` in the final fallback of `resolve_email_source()` so virtual job selectors never become registered-account source values.

- [ ] **Step 4: Run source tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_registration_email_source_selector.py tests/test_email_provider_gptmail.py tests/test_icloud_email_provider.py -q
```

Expected: all source catalog and existing fallback tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add core/email_provider.py tests/test_registration_email_source_selector.py tests/test_email_provider_gptmail.py tests/test_icloud_email_provider.py
git commit -m "feat: add registration email source selectors"
```

### Task 2: Filtered iCloud claims and persisted forced pickup modes

**Files:**
- Modify: `core/db.py`
- Modify: `core/icloud_mail_client.py`
- Modify: `tests/test_icloud_pool.py`
- Modify: `tests/test_icloud_mail_client.py`
- Modify: `tests/test_icloud_email_provider.py`

- [ ] **Step 1: Add failing pool tests for all/token/url filters**

Create pool records covering Token-only, URL-only, and Token + URL. Add tests equivalent to:

```python
token_claim = db.claim_next_icloud_email(pickup_filter="token")
self.assertTrue(token_claim["token"])
self.assertEqual(token_claim["claimed_pickup_mode"], "api_token")

url_claim = db.claim_next_icloud_email(pickup_filter="url")
self.assertTrue(url_claim["pickup_url"])
self.assertEqual(url_claim["claimed_pickup_mode"], "independent_url")
```

Verify:

- Token filter accepts Token-only and mixed records, but not URL-only.
- URL filter accepts URL-only and mixed records, but not Token-only.
- All filter keeps the stored/derived pickup mode.
- Two concurrent filtered claims never return the same mailbox.
- `release_icloud_email(..., status="available")` clears `claimed_pickup_mode`.
- `icloud_email_pool_summary(pickup_filter="token")` and `pickup_filter="url"` count only eligible available rows.
- Legacy camelCase `pickupUrl` is eligible for URL filtering.

- [ ] **Step 2: Run pool tests and verify RED**

Run:

```powershell
python -m pytest tests/test_icloud_pool.py -q
```

Expected: failures because claim/summary filters and `claimed_pickup_mode` are absent.

- [ ] **Step 3: Implement filtered atomic claims**

In `core/db.py`, add:

```python
def _icloud_row_pickup_url(row: dict) -> str:
    return str(row.get("pickup_url") or row.get("pickupUrl") or "").strip()

def _icloud_row_matches_filter(row: dict, pickup_filter: str) -> bool:
    mode = str(pickup_filter or "all").strip().lower()
    if mode == "token":
        return bool(str(row.get("token") or "").strip())
    if mode == "url":
        return bool(_icloud_row_pickup_url(row))
    if mode != "all":
        raise ValueError(f"未知 iCloud 领取过滤器: {mode}")
    return True
```

Change the claim signature:

```python
def claim_next_icloud_email(pickup_filter: str = "all") -> dict | None:
```

Select only `available` matching rows, then persist:

```python
if pickup_filter == "token":
    row["claimed_pickup_mode"] = "api_token"
elif pickup_filter == "url":
    row["claimed_pickup_mode"] = "independent_url"
else:
    row["claimed_pickup_mode"] = _icloud_pickup_mode(
        str(row.get("token") or "").strip(),
        _icloud_row_pickup_url(row),
    )
```

Add the same optional filter to `icloud_email_pool_summary()`. Clear `claimed_pickup_mode` whenever a claimed mailbox leaves `used` or is recycled to `available`.

- [ ] **Step 4: Add failing mail-client tests for forced API-only and URL-only behavior**

Add tests where the same mailbox contains both Token and browser URL:

```python
api_row = {
    "email": "mixed@icloud.com",
    "token": "tok_mixed",
    "pickup_url": "https://pickup.example/show/credential/mixed@icloud.com",
    "pickup_mode": "independent_url_with_token",
    "claimed_pickup_mode": "api_token",
}
```

Assert API forced mode:

- Calls JSON Pickup with Authorization.
- Does not request the independent HTML page.

Assert URL forced mode:

- Calls the independent HTML page.
- Sends no Authorization header.
- Does not call JSON Pickup or Profile even when Token/Profile configuration exists.

Also test `pick_account(selection="token")`, `pick_account(selection="url")`, and restoring context from DB after `_CONTEXT_CACHE.clear()`.

- [ ] **Step 5: Run client/provider tests and verify RED**

Run:

```powershell
python -m pytest tests/test_icloud_mail_client.py tests/test_icloud_email_provider.py -q
```

Expected: forced selection and persisted claimed-mode tests fail.

- [ ] **Step 6: Implement effective claimed modes in the client**

Change:

```python
def pick_account(selection: str = "all") -> ICloudMailAccount:
```

Map `selection` directly to the DB filter. When building an account, use:

```python
effective_mode = str(row.get("claimed_pickup_mode") or row.get("pickup_mode") or "api_token")
token = str(row.get("token") or "").strip()
pickup_url = str(row.get("pickup_url") or row.get("pickupUrl") or "").strip()

if effective_mode == "independent_url":
    token = ""
elif effective_mode == "api_token" and pickup_url:
    parsed = urlparse(pickup_url)
    path = parsed.path.rstrip("/").lower()
    if not (path.endswith("/messages/latest") or ("/api/" in path and path.endswith("/pickup"))):
        pickup_url = ""
```

Populate `ICloudMailAccount` with the effective values in both `pick_account()` and `get_account_context()`. Only perform Profile-based disabled-mailbox restoration for selections that permit Token use.

- [ ] **Step 7: Run all iCloud tests and commit Task 2**

Run:

```powershell
python -m pytest tests/test_icloud_pool.py tests/test_icloud_pickup_page.py tests/test_icloud_mail_client.py tests/test_icloud_email_provider.py -q
```

Expected: all tests pass.

Commit:

```powershell
git add core/db.py core/icloud_mail_client.py tests/test_icloud_pool.py tests/test_icloud_mail_client.py tests/test_icloud_email_provider.py
git commit -m "feat: filter icloud claims by pickup channel"
```

### Task 3: Make each registration job use its saved source snapshot

**Files:**
- Modify: `core/registration_service.py`
- Create: `tests/test_registration_service_email_source.py`
- Modify: `tests/test_registration_email_source_selector.py`

- [ ] **Step 1: Write failing worker isolation tests**

Create `tests/test_registration_service_email_source.py`. Patch the DB, executor, `main.run_registration`, and `core.email_provider.acquire_email` to prove:

```python
job = {
    "id": 7,
    "status": "pending",
    "email_source": "icloud_url",
    "log_file": str(temp_log),
}
```

When `_run_one_job(7, log_file)` runs, assert:

```python
acquire_email.assert_called_once_with("icloud_url")
```

Add a second test with two job records (`icloud_url` and `outlook`) and verify each worker passes its own source even if global `EMAIL_SOURCE` changes between job creation and execution.

Add tests that:

- `submit_registration(email_source=None)` snapshots the current normalized configuration string into every created job.
- `submit_registration(email_source="icloud_api_token")` writes that exact selector into every created job.
- Registration retry keeps the original job's selector.

- [ ] **Step 2: Run service tests and verify RED**

Run:

```powershell
python -m pytest tests/test_registration_service_email_source.py -q
```

Expected: workers still call `acquire_email()` without the saved job source.

- [ ] **Step 3: Pass job source through preparation and execution**

Change:

```python
def _prepare_registration_args(email_source: str | None = None) -> tuple[str, str, str]:
```

Use:

```python
email = acquire_email(email_source)
```

In `_run_one_job()`, read the already-loaded job snapshot:

```python
job_email_source = str(current.get("email_source") or "").strip()
email, name, birthday = _prepare_registration_args(job_email_source)
```

In `submit_registration()`, normalize once before taking the executor lock:

```python
from core.email_provider import snapshot_registration_source
email_source = snapshot_registration_source(email_source)
```

Update the outdated docstring that says the source is only recorded. Preserve `retry_job()` behavior so its copied `email_source` becomes the worker source automatically.

- [ ] **Step 4: Run service/source tests and commit Task 3**

Run:

```powershell
python -m pytest tests/test_registration_service_email_source.py tests/test_registration_email_source_selector.py -q
```

Expected: all tests pass.

Commit:

```powershell
git add core/registration_service.py tests/test_registration_service_email_source.py tests/test_registration_email_source_selector.py
git commit -m "fix: isolate registration job email sources"
```

### Task 4: Add source metadata API and selected-source validation

**Files:**
- Modify: `webui/app.py:2012-2138`
- Create: `tests/test_webui_registration_email_source.py`
- Modify: `tests/test_webui_icloud.py`
- Modify: `tests/test_webui_gptmail.py`
- Modify: `tests/test_webui_cloudflare.py`

- [ ] **Step 1: Write failing metadata endpoint tests**

Add tests for `GET /api/email-sources`:

```python
response = self.client.get("/api/email-sources")
payload = response.get_json()
self.assertEqual(payload["configured"], "icloud_api")
self.assertIn(
    {"value": "icloud_url", "label": "iCloud 独立 URL"},
    payload["options"],
)
self.assertNotIn("token", str(payload).lower())
self.assertNotIn("pickup_url", str(payload).lower())
```

The secret assertion should inspect keys/values for credential fields rather than rejecting the safe display word `Token` inside the label “iCloud API” if labels change.

- [ ] **Step 2: Write failing job-create tests for explicit source selection**

Cover:

```python
response = self.client.post("/api/jobs", json={
    "count": 2,
    "workers": 2,
    "email_source": "icloud_url",
})
submit_registration.assert_called_once_with(
    count=2,
    workers=2,
    email_source="icloud_url",
)
```

Also verify:

- `icloud_api_token` uses `icloud_email_pool_summary(pickup_filter="token")`.
- `icloud_url` uses `icloud_email_pool_summary(pickup_filter="url")`.
- `icloud_api` uses `pickup_filter="all"`.
- An empty selection snapshots the configured source and passes it explicitly to `submit_registration()`.
- Unknown or comma-separated explicit selection returns 400 and does not create tasks.
- GPTMail/MailNest/CloudMail/Cloudflare configuration checks use the selected source rather than unrelated global configuration.

- [ ] **Step 3: Run WebUI tests and verify RED**

Run:

```powershell
python -m pytest tests/test_webui_registration_email_source.py tests/test_webui_icloud.py tests/test_webui_gptmail.py tests/test_webui_cloudflare.py -q
```

Expected: metadata route is missing and `/api/jobs` ignores `email_source`.

- [ ] **Step 4: Extract selected-source validation and warning helpers**

In `webui/app.py`, add focused helpers near the existing pool helpers:

```python
def _selected_registration_source(raw_value) -> str:
    from core.email_provider import parse_email_sources, snapshot_registration_source
    raw = str(raw_value or "").strip()
    if not raw:
        return snapshot_registration_source()
    if any(separator in raw for separator in (",", ";", "|")):
        raise ValueError("前端每批注册只能选择一个邮箱来源")
    parsed = parse_email_sources(raw)
    if len(parsed) != 1 or parsed[0] != raw:
        raise ValueError("未知邮箱来源")
    return raw
```

Add helpers that receive `sources: list[str]` and perform the existing configuration checks and pool warning calculations. The iCloud warning branch must use the new filter argument.

- [ ] **Step 5: Add metadata route and wire POST source**

Add:

```python
@app.get("/api/email-sources")
def api_email_sources():
    from core.email_provider import registration_source_options, snapshot_registration_source
    return jsonify({
        "configured": snapshot_registration_source(),
        "options": registration_source_options(),
    })
```

In `api_jobs_create()`:

```python
try:
    selected_source = _selected_registration_source(data.get("email_source"))
except ValueError as exc:
    return jsonify({"ok": False, "error": str(exc)}), 400

sources = parse_email_sources(selected_source)
```

Use `sources` for configuration validation and capacity warnings, then call:

```python
jobs = svc.submit_registration(
    count=count,
    workers=workers,
    email_source=selected_source,
)
```

Return `email_source` and a safe display label in the JSON response.

- [ ] **Step 6: Update existing WebUI mock expectations**

Existing tests in `test_webui_gptmail.py`, `test_webui_cloudflare.py`, and `test_webui_icloud.py` currently expect:

```python
submit_registration.assert_called_once_with(count=1, workers=1)
```

Update them to include the selected/configured snapshot:

```python
submit_registration.assert_called_once_with(
    count=1,
    workers=1,
    email_source="gptmail",
)
```

Use the source actually configured in each test context.

- [ ] **Step 7: Run WebUI tests and commit Task 4**

Run:

```powershell
python -m pytest tests/test_webui_registration_email_source.py tests/test_webui_icloud.py tests/test_webui_gptmail.py tests/test_webui_cloudflare.py -q
```

Expected: all tests pass.

Commit:

```powershell
git add webui/app.py tests/test_webui_registration_email_source.py tests/test_webui_icloud.py tests/test_webui_gptmail.py tests/test_webui_cloudflare.py
git commit -m "feat: accept per-batch email sources"
```

### Task 5: Add the registration-page source selector

**Files:**
- Modify: `webui/templates/index.html:395-415,783-805`
- Create: `tests/test_webui_registration_email_source_template.py`

- [ ] **Step 1: Write failing template tests**

Assert the template contains:

```text
id="regEmailSource"
跟随当前配置
iCloud 全部
iCloud API
iCloud 独立 URL
/api/email-sources
email_source: selectedSource
```

Also assert the selector does not contain “URL + API 后备”.

- [ ] **Step 2: Run template tests and verify RED**

Run:

```powershell
python -m pytest tests/test_webui_registration_email_source_template.py -q
```

Expected: the selector and initialization code are absent.

- [ ] **Step 3: Add the HTML selector and safe fallback options**

Add a field before the count input:

```html
<label class="fld">邮箱来源
  <select id="regEmailSource">
    <option value="">跟随当前配置</option>
    <option value="icloud_api">iCloud 全部</option>
    <option value="icloud_api_token">iCloud API</option>
    <option value="icloud_url">iCloud 独立 URL</option>
    <option value="outlook">Outlook</option>
    <option value="generic_api">通用 API</option>
    <option value="cloudflare_domain">Cloudflare 域名邮箱</option>
    <option value="cloudflare">Cloudflare 临时邮箱</option>
    <option value="gptmail">GPTMail</option>
    <option value="mailnest">MailNest</option>
    <option value="cloudmail">CloudMail</option>
  </select>
  <span class="hint" id="regEmailSourceHint">空值将跟随当前配置</span>
</label>
```

- [ ] **Step 4: Load source metadata and submit the selection**

Add:

```javascript
let REG_EMAIL_SOURCE_LABELS = {};

async function loadRegistrationEmailSources() {
  const response = await api('/api/email-sources');
  const select = $('#regEmailSource');
  const options = Array.isArray(response.options) ? response.options : [];
  REG_EMAIL_SOURCE_LABELS = Object.fromEntries(options.map(item => [item.value, item.label]));
  if (options.length) {
    select.innerHTML = '<option value="">跟随当前配置</option>' +
      options.map(item => `<option value="${esc(item.value)}">${esc(item.label)}</option>`).join('');
  }
  $('#regEmailSourceHint').textContent = `当前配置：${response.configured || '-'}`;
}
```

In the start handler:

```javascript
const selectedSource = $('#regEmailSource').value || '';
body: JSON.stringify({count, workers, email_source: selectedSource})
```

Show the returned safe source label in the success/warning banner. Call `loadRegistrationEmailSources()` during initialization. Do not persist the temporary selection in `localStorage`.

- [ ] **Step 5: Run template and WebUI tests**

Run:

```powershell
python -m pytest tests/test_webui_registration_email_source_template.py tests/test_webui_registration_email_source.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add webui/templates/index.html tests/test_webui_registration_email_source_template.py
git commit -m "feat: select email source on registration page"
```

### Task 6: Integration, concurrency regression, and final verification

**Files:**
- Modify if required by failures: only files already listed in Tasks 1-5
- Test: full `tests/` suite

- [ ] **Step 1: Run all focused tests**

Run:

```powershell
python -m pytest tests/test_registration_email_source_selector.py tests/test_registration_service_email_source.py tests/test_webui_registration_email_source.py tests/test_webui_registration_email_source_template.py tests/test_icloud_pool.py tests/test_icloud_pickup_page.py tests/test_icloud_mail_client.py tests/test_icloud_email_provider.py tests/test_webui_icloud.py tests/test_webui_gptmail.py tests/test_webui_cloudflare.py tests/test_sms_provider_sms_activate.py -q
```

Expected: all focused tests pass, including HeroSMS regression coverage.

- [ ] **Step 2: Run a deterministic concurrent-source test**

The service test must create two runnable job snapshots:

```text
job A -> icloud_url
job B -> outlook
```

Run both through a two-worker `ThreadPoolExecutor` with mocked providers and assert each job receives only its selected provider result. Repeat the test enough times inside one test case to detect accidental global-state dependence without making network calls.

Run:

```powershell
python -m pytest tests/test_registration_service_email_source.py -q
```

Expected: the concurrent isolation test passes consistently.

- [ ] **Step 3: Run the complete regression suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Compile and scan for credential leakage**

Run:

```powershell
python -m compileall -q core webui tests
git diff --check
git diff --cached --check
git grep -n "icloud-api.top/show/" -- ':!docs/superpowers/specs/*' ':!docs/superpowers/plans/*'
git status --short --branch
```

Expected:

- Compilation succeeds.
- No diff errors.
- No real independent URL is tracked.
- `.env` is unchanged.
- Only the pre-existing untracked `logs/` directory remains outside intended changes.

- [ ] **Step 5: Request code review**

Review the complete range from the commit before Task 1 through the final implementation commit. Required review focus:

- Per-job source snapshot is used at execution time.
- No global `EMAIL_SOURCE` mutation occurs.
- API/URL forced iCloud modes do not fall back across channels.
- Concurrent claims remain atomic.
- Existing retries inherit source selection.
- Source metadata API returns no secrets.

Fix all Critical and Important findings with new failing tests before continuing.

- [ ] **Step 6: Commit final review fixes if needed**

```powershell
git add core webui tests
git commit -m "fix: harden registration source isolation"
```

Skip this commit only when the review produces no code changes.

- [ ] **Step 7: Verify final branch state**

Run:

```powershell
python -m pytest -q
git status --short --branch
git log -10 --oneline
```

Expected: full suite passes, implementation commits are present on `codex/hero-sms-provider`, and only `logs/` remains untracked.
