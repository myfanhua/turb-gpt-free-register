# Roxy Codex Token Proxy Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Codex token exchange reuse the exact Roxy proxy bridge and fail before paid SMS acquisition when that route is unavailable.

**Architecture:** Resolve the effective token proxy from `RoxyOpenResult.registration_proxy`, create one HTTP session before browser login, perform a transport preflight, and reuse that session for the final exchange. Extend the configured sticky-session window from three to ten minutes while preserving per-task SID rotation.

**Tech Stack:** Python 3, curl_cffi, Selenium/RoxyBrowser, unittest/pytest, dotenv configuration.

---

### Task 1: Proxy selection and preflight

**Files:**
- Modify: `core/roxy_codex_oauth.py`
- Create: `tests/test_roxy_codex_token_proxy.py`

- [ ] Write failing tests proving the Roxy registration proxy takes priority, an explicit proxy is the fallback, and preflight calls the token endpoint.
- [ ] Run `python -m pytest tests/test_roxy_codex_token_proxy.py -q` and confirm the tests fail because the helpers do not exist.
- [ ] Add `_resolve_token_exchange_proxy` and `_preflight_token_exchange_transport`.
- [ ] Create the local-mode token session before `_fill_email_and_otp` and reuse it in `exchange_codex_token`.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Sticky-session duration

**Files:**
- Modify: `.env`

- [ ] Replace `-t-3` with `-t-10` in `PROXY_POOL` and `PROXY_CHAIN_UPSTREAM`.
- [ ] Keep `{sid}` unchanged so every task still receives a distinct proxy session.
- [ ] Restart WebUI port 5002 so the in-process proxy bridge loads the new template.

### Task 3: Verification and commit

**Files:**
- Test: `tests/test_roxy_codex_token_proxy.py`
- Test: full repository test suite

- [ ] Run `python -m pytest -q` and confirm all automated tests pass.
- [ ] Run `git diff --check`.
- [ ] Commit source, tests, design, and plan without adding `.env` or `logs/`.
- [ ] Do not trigger a real Codex retry or acquire a phone number.
