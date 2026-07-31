# Roxy Clash Proxy Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route RoxyBrowser traffic through Clash `127.0.0.1:7897` to the account-authenticated Korean upstream proxy while preserving one SID per Roxy profile.

**Architecture:** Add a local threaded HTTP proxy bridge. Roxy receives a local proxy URL whose username carries the generated SID; the bridge parses that username, opens a CONNECT tunnel to the upstream HTTP proxy through Clash, replaces local auth with upstream auth, and relays bytes. The bridge is started on demand before Roxy profile creation and reused while the process is alive.

**Tech Stack:** Python 3.13, `socket`, `socketserver`, `select`, `requests`, existing config/env loader, unittest.

---

### Task 1: Bridge protocol helpers

**Files:**
- Create: `tools/proxy_chain_bridge.py`
- Create: `tests/test_proxy_chain_bridge.py`

- [ ] Write failing tests for parsing upstream/local proxy URLs, extracting SID from local Basic auth, and replacing `Proxy-Authorization` without exposing credentials.
- [ ] Run `python -m unittest tests.test_proxy_chain_bridge -v` and confirm the new tests fail because the bridge module is absent.
- [ ] Implement pure helpers: `ProxyEndpoint`, `parse_proxy_url`, `build_proxy_authorization`, `extract_local_sid`, `rewrite_proxy_authorization`, `build_connect_request`.
- [ ] Run the targeted tests and confirm they pass.

### Task 2: Bridge CONNECT relay

**Files:**
- Modify: `tools/proxy_chain_bridge.py`
- Modify: `tests/test_proxy_chain_bridge.py`

- [ ] Add a fake Clash server test that accepts CONNECT, then verifies the bridge sends an upstream CONNECT with the upstream credentials and generated SID.
- [ ] Implement `_connect_via_chain`, bounded header reads, bidirectional `select` relay, and a threaded `BridgeServer` handler for CONNECT and absolute-form HTTP requests.
- [ ] Map bridge failures to `502` responses with stable markers `CLASH_PREPROXY_UNAVAILABLE`, `UPSTREAM_PROXY_AUTH_FAILED`, or `SID_MISSING`.
- [ ] Run the bridge tests and confirm all pass.

### Task 3: Config and lifecycle

**Files:**
- Modify: `config/proxy.py`
- Modify: `core/roxybrowser_client.py`
- Create: `tests/test_proxy_bridge_config.py`

- [ ] Write failing tests for bridge settings defaults, local proxy URL generation with per-call SID, and idempotent bridge startup.
- [ ] Run the targeted tests and confirm failure.
- [ ] Add `PROXY_CHAIN_ENABLED`, `PROXY_CHAIN_LISTEN_HOST`, `PROXY_CHAIN_LISTEN_PORT`, `PROXY_CHAIN_PREPROXY`, and `PROXY_CHAIN_UPSTREAM` settings. Keep existing `{sid}` generation and credentials in `.env`.
- [ ] Add `ensure_proxy_chain()` in the Roxy client. It starts the bridge once per process and returns a local authenticated proxy URL for the current SID.
- [ ] Update profile creation so `ROXY_CREATE_USE_PROXY_POOL=true` passes the local bridge URL while the bridge receives the upstream template.
- [ ] Run config/lifecycle tests and confirm all pass.

### Task 4: Runtime verification

**Files:**
- Modify: `.env` (local ignored configuration only)
- Modify: `README.md` only if the runtime command needs documentation

- [ ] Start the bridge through the project runtime and verify the local port listens.
- [ ] Verify the bridge can reach the upstream proxy through Clash using an IP endpoint, without printing credentials.
- [ ] Create two temporary Roxy profiles and verify their local proxy usernames contain different SIDs.
- [ ] Run the full relevant test set and perform a Roxy smoke test showing ChatGPT static resources no longer return `ERR_CONNECTION_CLOSED`.
- [ ] Commit implementation and test changes.
