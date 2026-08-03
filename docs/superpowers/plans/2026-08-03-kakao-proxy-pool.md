# Kakao Proxy Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Kakao extract-link provider use the existing Korean `PROXY_POOL` by default while keeping one selected proxy fixed for each submitted batch and its polling requests.

**Architecture:** Add one default-on boolean setting to the existing extract-link configuration. `extract_link_service` selects one proxy through `config.proxy.pick_proxy()` whenever it creates a Kakao client; the client owns that proxy for its lifetime and passes it through both the `curl_cffi` and standard-library HTTP transports. Empty pools and selection errors fall back to direct routing.

**Tech Stack:** Python 3.13, Flask configuration editor, `curl_cffi.requests`, `urllib.request`, `unittest`/`pytest`.

---

## File map

- Modify `config/extract_link.py`: define and load the default-on proxy-pool switch.
- Modify `webui/config_editor.py`: expose the switch in the existing 提链 configuration group.
- Modify `core/kakao_extract_link_provider.py`: keep a batch-scoped proxy and apply it to both HTTP transport implementations.
- Modify `core/extract_link_service.py`: select one proxy whenever one Kakao client/batch is created.
- Modify `tests/test_kakao_extract_link_provider.py`: verify the transport receives the fixed proxy and standard-library fallback installs it.
- Modify `tests/test_extract_link_provider_service.py`: verify enabled, disabled, empty-pool, and selection-error routing.
- Modify `tests/test_webui_extract_link_provider_template.py`: verify the new configuration switch is available and defaults to enabled.

### Task 1: Add the default-on configuration switch

**Files:**
- Modify: `tests/test_webui_extract_link_provider_template.py`
- Modify: `config/extract_link.py`
- Modify: `webui/config_editor.py`

- [ ] **Step 1: Write the failing configuration test**

Add a test that imports `config.extract_link`, locates `KAKAO_EXTRACT_USE_PROXY_POOL` in `webui.config_editor.EDITABLE_FIELDS`, and asserts both the Python default and field type:

```python
def test_kakao_proxy_pool_switch_defaults_on(self):
    from config import extract_link
    from webui.config_editor import EDITABLE_FIELDS

    field = next(
        item for item in EDITABLE_FIELDS
        if item.get("key") == "KAKAO_EXTRACT_USE_PROXY_POOL"
    )
    self.assertTrue(extract_link.KAKAO_EXTRACT_USE_PROXY_POOL)
    self.assertEqual(field["type"], "bool")
    self.assertEqual(field["group"], "提链")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_webui_extract_link_provider_template.py::WebUIExtractLinkProviderTemplateTests::test_kakao_proxy_pool_switch_defaults_on -q
```

Expected: FAIL because `KAKAO_EXTRACT_USE_PROXY_POOL` and its editor field do not exist.

- [ ] **Step 3: Implement the switch**

Add to `config/extract_link.py`:

```python
KAKAO_EXTRACT_USE_PROXY_POOL: bool = True
```

and register it in `apply_env_overrides`:

```python
'KAKAO_EXTRACT_USE_PROXY_POOL': 'bool',
```

Add to the 提链 group in `webui/config_editor.py`:

```python
{
    "key": "KAKAO_EXTRACT_USE_PROXY_POOL",
    "file": "extract_link.py",
    "type": "bool",
    "group": "提链",
    "label": "Kakao API·使用代理池",
    "help": "默认开启；每个 Kakao 批次从现有代理池抽取一次，提交和轮询固定使用同一代理",
},
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command again. Expected: `1 passed`.

- [ ] **Step 5: Commit the configuration slice**

```powershell
git add config/extract_link.py webui/config_editor.py tests/test_webui_extract_link_provider_template.py
git commit -m "feat: add Kakao proxy pool switch"
```

### Task 2: Apply one fixed proxy inside the Kakao client

**Files:**
- Modify: `tests/test_kakao_extract_link_provider.py`
- Modify: `core/kakao_extract_link_provider.py`

- [ ] **Step 1: Write failing transport tests**

Extend the test helper so it accepts `proxy`, then add:

```python
def test_curl_transport_reuses_client_proxy_for_submit_and_poll(self):
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload

    class FakeCurl:
        @staticmethod
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs.get("proxy")))
            payload = (
                {"batchId": "batch-1", "status": "queued"}
                if method == "POST"
                else {"batchId": "batch-1", "status": "completed", "done": True, "results": []}
            )
            return FakeResponse(payload)

    with patch("core.kakao_extract_link_provider.curl_requests", FakeCurl):
        client = self.make_client(None, proxy="http://user:pass@kr.proxy:9000")
        client.submit(["TOKEN_A"])
        client.get_batch("batch-1")

    self.assertEqual([item[2] for item in calls], [
        "http://user:pass@kr.proxy:9000",
        "http://user:pass@kr.proxy:9000",
    ])
```

Add a standard-library fallback test that patches `curl_requests` to `None`, patches `ProxyHandler` and `build_opener`, calls `_default_transport`, and asserts the handler receives:

```python
{
    "http": "http://user:pass@kr.proxy:9000",
    "https": "http://user:pass@kr.proxy:9000",
}
```

- [ ] **Step 2: Run both tests and verify RED**

Run:

```powershell
python -m pytest tests/test_kakao_extract_link_provider.py -q
```

Expected: FAIL because `KakaoExtractLinkClient` has no `proxy` argument and transports do not pass a proxy.

- [ ] **Step 3: Implement batch-scoped proxy transport**

In `core/kakao_extract_link_provider.py`:

```python
from urllib.request import ProxyHandler, Request, build_opener, urlopen
```

Add `proxy: str = ""` to the client constructor and store:

```python
self.proxy = str(proxy or "").strip()
```

Convert `_default_transport` from a static method to a normal instance method. In the `curl_cffi` call include:

```python
proxy=self.proxy or None,
```

For the standard-library path use the existing `urlopen` when no proxy is selected; when selected, use:

```python
handler = ProxyHandler({"http": self.proxy, "https": self.proxy})
opener = build_opener(handler)
response_context = opener.open(request, timeout=timeout)
```

Keep the existing JSON parsing and context-manager handling unchanged.

- [ ] **Step 4: Run provider tests and verify GREEN**

Run the Step 2 command again. Expected: all tests in the file pass.

- [ ] **Step 5: Commit the transport slice**

```powershell
git add core/kakao_extract_link_provider.py tests/test_kakao_extract_link_provider.py
git commit -m "feat: route Kakao client through a fixed proxy"
```

### Task 3: Select one proxy for every Kakao batch

**Files:**
- Modify: `tests/test_extract_link_provider_service.py`
- Modify: `core/extract_link_service.py`

- [ ] **Step 1: Write failing service-routing tests**

Add tests for `_make_kakao_client` using patched `KakaoExtractLinkClient` and `config.proxy.pick_proxy`:

```python
def test_make_kakao_client_uses_proxy_pool_by_default(self):
    with patch.object(
        extract_link_service,
        "_runtime_setting",
        side_effect=lambda name, default=None: True
        if name == "KAKAO_EXTRACT_USE_PROXY_POOL" else default,
    ), patch(
        "config.proxy.pick_proxy",
        return_value="http://user:pass@kr.proxy:9000",
    ) as pick, patch.object(
        extract_link_service,
        "KakaoExtractLinkClient",
    ) as client_type:
        extract_link_service._make_kakao_client(cdk="CDK")

    pick.assert_called_once_with()
    self.assertEqual(client_type.call_args.kwargs["proxy"], "http://user:pass@kr.proxy:9000")
```

Add separate tests asserting:

- the switch set to `False` does not call `pick_proxy` and passes `proxy=""`;
- `pick_proxy()` returning `""` passes direct routing;
- `pick_proxy()` raising an exception passes direct routing rather than raising.

- [ ] **Step 2: Run the service test file and verify RED**

Run:

```powershell
python -m pytest tests/test_extract_link_provider_service.py -q
```

Expected: FAIL because `_make_kakao_client` does not read the switch or pass `proxy`.

- [ ] **Step 3: Implement proxy selection with direct fallback**

Add a boolean parser:

```python
def _bool_setting(name: str, default: bool) -> bool:
    raw = _runtime_setting(name, default)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}
```

Add selection:

```python
def _kakao_batch_proxy() -> str:
    if not _bool_setting("KAKAO_EXTRACT_USE_PROXY_POOL", True):
        return ""
    try:
        from config.proxy import pick_proxy
        return str(pick_proxy() or "").strip()
    except Exception as exc:
        logger.warning("[提链] Kakao 代理池抽取失败，本批改为直连: %s", type(exc).__name__)
        return ""
```

Pass `proxy=_kakao_batch_proxy()` to `KakaoExtractLinkClient`. Since `_make_kakao_client` is already invoked inside each `for plan in plans` iteration and once for every resumed batch, this selects exactly once per batch while the returned client keeps the route fixed for submit and polling.

Log only the route mode after client creation:

```python
logger.info("[提链] Kakao 批次网络: %s", "proxy" if client.proxy else "direct")
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_extract_link_provider_service.py tests/test_kakao_extract_link_provider.py tests/test_webui_extract_link_provider_template.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit the routing slice**

```powershell
git add core/extract_link_service.py tests/test_extract_link_provider_service.py
git commit -m "feat: use proxy pool for each Kakao batch"
```

### Task 4: Regression verification

**Files:**
- Verify all modified files and the existing test suite.

- [ ] **Step 1: Run extract-link and proxy regression tests**

```powershell
python -m pytest tests/test_kakao_extract_link_provider.py tests/test_extract_link_provider_service.py tests/test_webui_extract_link_provider.py tests/test_webui_extract_link_provider_template.py tests/test_proxy_rotation.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the full suite**

```powershell
python -m pytest -q
```

Expected: zero failures.

- [ ] **Step 3: Validate repository cleanliness**

```powershell
git diff --check
git status --short
```

Expected: `git diff --check` has no output; only the intentionally preserved untracked `logs/` may remain.

- [ ] **Step 4: Review the final diff**

Confirm that the diff contains only the new Kakao proxy-pool setting, its UI field, proxy transport wiring, service selection, and tests. Do not restart WebUI during implementation verification.
