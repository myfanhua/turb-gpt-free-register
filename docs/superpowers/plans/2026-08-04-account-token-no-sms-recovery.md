# Account Token No-SMS Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep all account plan checks on the account's saved registration proxy without adding any automatic Codex/SMS recovery path.

**Architecture:** Resolve the effective proxy at each WebUI request boundary: explicit request values override stored values, otherwise each account supplies its own `proxy_used`. The Codex Plus gate passes the same stored proxy into the synchronous plan checker. Existing mailbox refresh credentials remain isolated from ChatGPT access-token handling.

**Tech Stack:** Python 3, Flask, unittest, unittest.mock

---

### Task 1: Lock WebUI proxy selection behavior

**Files:**
- Create: `tests/test_webui_plan_check_proxy.py`
- Modify: `webui/app.py`

- [ ] Write route tests proving single-account and bulk queries use stored account proxies when the JSON body omits `proxy`.
- [ ] Add tests proving an explicitly supplied `proxy` value continues to override stored proxies.
- [ ] Run `python -m pytest tests/test_webui_plan_check_proxy.py -q` and confirm the new default-proxy assertions fail.
- [ ] Update the single and bulk routes so only an explicit request proxy overrides `account["proxy_used"]`.
- [ ] Re-run `python -m pytest tests/test_webui_plan_check_proxy.py -q` and confirm all tests pass.

### Task 2: Lock Codex gate proxy continuity

**Files:**
- Modify: `tests/test_codex_retry_plus_gate.py`
- Modify: `core/codex_retry_service.py`

- [ ] Update the Plus gate test account to include a saved proxy and require that proxy in the synchronous plan-check call.
- [ ] Run `python -m pytest tests/test_codex_retry_plus_gate.py -q` and confirm the proxy assertion fails.
- [ ] Pass `account.get("proxy_used") or None` into `check_account_plan_now`.
- [ ] Re-run `python -m pytest tests/test_codex_retry_plus_gate.py -q` and confirm all tests pass.

### Task 3: Regression verification

**Files:**
- Verify only

- [ ] Run the focused proxy and Codex tests.
- [ ] Run the complete pytest suite.
- [ ] Run `git diff --check` and inspect `git status --short` so runtime logs remain untracked and uncommitted.
- [ ] Commit only the design, plan, tests, and implementation files on `codex/hero-sms-provider`.

### Task 4: Restore persisted local bridges after service restart

**Files:**
- Modify: `core/plan_check_service.py`
- Modify: `tests/test_plan_check_sync.py`
- Modify: `tests/test_plan_check_same_proxy.py`

- [ ] Add failing synchronous and background-query tests requiring a saved `sid-*:bridge@127.0.0.1:25001` proxy to call `prepare_proxy_for_roxy` before the request.
- [ ] Detect saved local bridge URLs from the configured bridge host, port, SID username, and `bridge` password.
- [ ] Start or reuse the configured proxy-chain bridge and pass the prepared URL to both the initial query and registration recheck.
- [ ] Run the focused tests and full suite before restarting the WebUI service.

### Task 5: Preserve configured country when live location lookup fails

**Files:**
- Modify: `core/registration_location.py`
- Modify: `core/account_export.py`
- Modify: `tests/test_registration_location.py`
- Modify: `tests/test_account_export_plan_proxy.py`

- [ ] Add failing tests for `region-XX` extraction from direct proxies and saved local bridge proxies.
- [ ] Infer the country code from the configured upstream template only when live lookup returned no country code.
- [ ] Persist only the inferred country code; keep region and IP empty.
- [ ] Backfill account 168 with country code `US` while preserving its missing historical IP.

### Task 6: Persist one device identity per account

**Files:**
- Modify: `core/session.py`
- Modify: `core/roxybrowser_client.py`
- Modify: `core/roxy_registration.py`
- Modify: `core/account_export.py`
- Modify: `core/chatgpt_plan.py`
- Modify: `core/plan_check_service.py`
- Modify: `core/roxy_codex_oauth.py`
- Modify: `webui/app.py`
- Test: `tests/test_account_device_context.py`
- Test: `tests/test_webui_plan_check_proxy.py`
- Test: `tests/test_plan_check_sync.py`

- [ ] Add failing tests proving a supplied `device_id` is retained by `BrowserSession` and written as `oai-did` cookies in Roxy.
- [ ] Generate one device ID before the first Roxy registration navigation and save the same value with the account.
- [ ] Thread the saved device ID through single, bulk, automatic and synchronous plan checks.
- [ ] Stop manual and automatic token checks when an existing account has no saved device context.
- [ ] Inject the saved device ID into every later Roxy OAuth environment before navigation, including after browser-state clearing.
- [ ] Pass the same device ID to Codex token-exchange HTTP sessions.
- [ ] Run focused tests, full tests, commit and restart the WebUI service without starting real registration or SMS tasks.
