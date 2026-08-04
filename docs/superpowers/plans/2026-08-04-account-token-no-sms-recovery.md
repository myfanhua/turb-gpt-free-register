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
