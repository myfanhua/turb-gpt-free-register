# Kakao Extract Link Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the existing SSE extract-link service while adding a selectable Kakao asynchronous batch provider with user-controlled batches of one to five selected accounts.

**Architecture:** Keep `core/extract_link_service.py` as the orchestration boundary, isolate the new HTTP contract in `core/kakao_extract_link_provider.py`, and extend the JSON account store with provider/batch metadata needed for safe restart recovery. Existing WebUI routes remain compatible while accepting provider and batch-size overrides; the account toolbar exposes the common controls and the configuration page keeps both providers' credentials isolated.

**Tech Stack:** Python 3, Flask, curl_cffi with urllib fallback, JSON file persistence, vanilla JavaScript/HTML, unittest/pytest.

---

### Task 1: Add provider configuration and validation

**Files:**
- Modify: `config/extract_link.py`
- Modify: `webui/config_editor.py`
- Create: `tests/test_kakao_extract_config.py`

- [ ] **Step 1: Write failing configuration tests**

Create tests that require the new defaults and editable fields:

```python
import unittest

from config import extract_link
from webui.config_editor import EDITABLE_FIELDS


class KakaoExtractConfigTests(unittest.TestCase):
    def test_defaults_preserve_legacy_provider(self):
        self.assertEqual(extract_link.EXTRACT_LINK_PROVIDER, "legacy")
        self.assertEqual(extract_link.KAKAO_EXTRACT_BATCH_SIZE, 5)
        self.assertEqual(extract_link.KAKAO_EXTRACT_API_BASE, "https://tiqu.dxmcs.xin")
        self.assertEqual(extract_link.KAKAO_EXTRACT_TIMEOUT_SECONDS, 930)
        self.assertEqual(extract_link.KAKAO_EXTRACT_POLL_INTERVAL, 4.0)

    def test_kakao_cdk_is_a_separate_secret_field(self):
        fields = {field["key"]: field for field in EDITABLE_FIELDS}
        self.assertTrue(fields["EXTRACT_LINK_CDK"]["secret"])
        self.assertTrue(fields["KAKAO_EXTRACT_CDK"]["secret"])
        self.assertNotEqual(
            fields["EXTRACT_LINK_CDK"]["key"],
            fields["KAKAO_EXTRACT_CDK"]["key"],
        )
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_kakao_extract_config.py -q
```

Expected: failures because the Kakao configuration constants and editor fields do not exist.

- [ ] **Step 3: Add minimal configuration constants**

Add to `config/extract_link.py` and its `apply_env_overrides` map:

```python
EXTRACT_LINK_PROVIDER: str = "legacy"
KAKAO_EXTRACT_API_BASE: str = "https://tiqu.dxmcs.xin"
KAKAO_EXTRACT_CDK: str = ""
KAKAO_EXTRACT_BATCH_SIZE: int = 5
KAKAO_EXTRACT_TIMEOUT_SECONDS: int = 930
KAKAO_EXTRACT_POLL_INTERVAL: float = 4.0
```

Add editable fields under the existing `提链` group. Mark both CDKs as `storage="env"` and `secret=True`. Use labels prefixed with “通用”“旧接口” and “Kakao API” so the frontend can divide them into three subsections without introducing extra top-level tabs.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_kakao_extract_config.py tests/test_config_defaults.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit configuration support**

```powershell
git add config/extract_link.py webui/config_editor.py tests/test_kakao_extract_config.py
git commit -m "feat: add kakao extract provider configuration"
```

---

### Task 2: Implement the Kakao API client

**Files:**
- Create: `core/kakao_extract_link_provider.py`
- Create: `tests/test_kakao_extract_link_provider.py`

- [ ] **Step 1: Write failing request and response tests**

Tests must cover payload construction, `batchId` validation, status polling, error-code translation, secret redaction, and transient GET retry. The desired public surface is:

```python
client = KakaoExtractLinkClient(
    api_base="https://tiqu.dxmcs.xin",
    cdk="KAKAO-CDK",
    timeout_seconds=930,
    poll_interval=0,
    transport=fake_transport,
    sleep=lambda _: None,
)

accepted = client.submit(["TOKEN_A", "TOKEN_B"])
self.assertEqual(accepted.batch_id, "batch-1")

completed = client.poll("batch-1")
self.assertTrue(completed.done)
self.assertEqual(completed.results[0]["paymentLink"], "https://pay.example/1")
```

The fake transport should assert that submit uses:

```python
{
    "accessTokens": ["TOKEN_A", "TOKEN_B"],
    "cdk": "KAKAO-CDK",
    "timeoutSeconds": 930,
}
```

- [ ] **Step 2: Run the client tests and verify RED**

```powershell
python -m pytest tests/test_kakao_extract_link_provider.py -q
```

Expected: import failure because `core.kakao_extract_link_provider` does not exist.

- [ ] **Step 3: Implement the minimal client**

Create focused dataclasses and client methods:

```python
@dataclass(frozen=True)
class KakaoAcceptedBatch:
    batch_id: str
    request_id: str = ""
    status: str = "queued"


@dataclass(frozen=True)
class KakaoBatchResult:
    batch_id: str
    status: str
    done: bool
    results: list[dict]
    success_count: int = 0
    failure_count: int = 0
    charged_count: int = 0
    remaining_count: int | None = None
```

`KakaoExtractLinkClient.submit(tokens)` must call `POST /api/v1/extractions/async`, reject an empty list or more than five tokens, and require `batchId`. `poll(batch_id)` must call `GET /api/v1/extractions/{quoted_batch_id}` until terminal status or the local deadline. Only GET polling receives bounded transient retries; POST response uncertainty is surfaced without blind resubmission.

The module must translate documented errors and sanitize exception messages so Access Tokens, CDKs, and authenticated URLs are absent from logs and WebUI responses.

Use these explicit translations:

```python
ERROR_MESSAGES = {
    "CDK_INVALID": "CDK 无效、已停用或已过期",
    "CDK_QUOTA_EXHAUSTED": "CDK 次数已用完",
    "CDK_QUOTA_INSUFFICIENT": "CDK 剩余次数不足以覆盖本批账号",
}
```

HTTP 404 containing `batch not found` becomes “批次不存在或已被服务端清理”; HTTP 422 keeps the server's bounded validation detail.

- [ ] **Step 4: Run client tests and verify GREEN**

```powershell
python -m pytest tests/test_kakao_extract_link_provider.py -q
```

Expected: all client tests pass.

- [ ] **Step 5: Commit the client**

```powershell
git add core/kakao_extract_link_provider.py tests/test_kakao_extract_link_provider.py
git commit -m "feat: add kakao batch extraction client"
```

---

### Task 3: Add deterministic batch planning and result mapping

**Files:**
- Modify: `core/kakao_extract_link_provider.py`
- Modify: `tests/test_kakao_extract_link_provider.py`

- [ ] **Step 1: Write failing pure-function tests**

Require these behaviors:

```python
items = [
    {"account_id": 1, "email": "a@example.com", "access_token": "TOKEN_A"},
    {"account_id": 2, "email": "b@example.com", "access_token": "TOKEN_A"},
    {"account_id": 3, "email": "c@example.com", "access_token": "TOKEN_C"},
]

batches = build_kakao_batches(items, batch_size=2)
self.assertEqual(batches[0].tokens, ["TOKEN_A", "TOKEN_C"])
self.assertEqual(batches[0].account_ids_by_result_index[0], [1, 2])
```

Also assert that 12 unique tokens with `batch_size=5` produce batch lengths `[5, 5, 2]`, values outside `1～5` are rejected, partial success is mapped independently, and a result-count mismatch leaves unmatched accounts failed instead of shifting later results.

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
python -m pytest tests/test_kakao_extract_link_provider.py -q
```

Expected: failures because the batch planner and result mapper do not exist.

- [ ] **Step 3: Implement pure batch structures and mapping**

Add a batch plan carrying only in-memory tokens and persistent-safe metadata:

```python
@dataclass(frozen=True)
class KakaoBatchPlan:
    batch_number: int
    batch_total: int
    tokens: list[str]
    account_ids_by_result_index: dict[int, list[int]]


def build_kakao_batches(items: list[dict], batch_size: int) -> list[KakaoBatchPlan]:
    ...


def map_kakao_results(
    plan: KakaoBatchPlan,
    results: list[dict],
) -> dict[int, dict]:
    ...
```

Deduplicate by exact token while preserving first-seen order. Never persist or return the `tokens` list. Successful Kakao results normalize `paymentLink` into the existing `long_url` field and set `payment_method="kakao_pay"` and `payment_link_type="kakao"`.

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
python -m pytest tests/test_kakao_extract_link_provider.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit batch planning**

```powershell
git add core/kakao_extract_link_provider.py tests/test_kakao_extract_link_provider.py
git commit -m "feat: plan kakao extract batches safely"
```

---

### Task 4: Persist provider metadata and support restart recovery

**Files:**
- Modify: `core/db.py`
- Create: `tests/test_kakao_extract_db.py`

- [ ] **Step 1: Write failing persistence tests**

Patch `db._load_accounts` and `db._save_accounts` with an in-memory list. Require `claim_account_extract` and `update_account_extract` to preserve:

```python
{
    "extract_link_provider": "kakao_batch",
    "extract_link_batch_id": "batch-1",
    "extract_link_batch_number": 1,
    "extract_link_batch_total": 3,
    "extract_link_result_index": 0,
}
```

Require restart recovery to return one descriptor per distinct Kakao `batchId`, keep those rows recoverable, and mark legacy or Kakao rows without a `batchId` as failed.

- [ ] **Step 2: Run the DB tests and verify RED**

```powershell
python -m pytest tests/test_kakao_extract_db.py -q
```

Expected: failures because provider metadata and grouped recovery descriptors are not supported.

- [ ] **Step 3: Extend atomic claim/update behavior**

Extend `claim_account_extract` with optional keyword-only metadata while preserving existing callers:

```python
def claim_account_extract(
    acc_id: int,
    trigger: str = "manual",
    link_type: str = "pix",
    *,
    provider: str = "legacy",
    batch_number: int | None = None,
    batch_total: int | None = None,
    result_index: int | None = None,
) -> bool:
    ...
```

Teach `update_account_extract` to store `provider`, `batch_id`, batch numbering, result index, `charged_count`, and `cdk_remaining`. Add a release helper that resets only queued claims when executor submission fails.

Change recovery to return a structure such as:

```python
{
    "failed_count": 2,
    "kakao_batches": [
        {
            "batch_id": "batch-1",
            "accounts": [
                {"account_id": 10, "result_index": 0},
                {"account_id": 11, "result_index": 1},
            ],
            "batch_number": 1,
            "batch_total": 2,
        }
    ],
}
```

No complete Access Token is written into recovery metadata.

- [ ] **Step 4: Run DB tests and verify GREEN**

```powershell
python -m pytest tests/test_kakao_extract_db.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit persistence changes**

```powershell
git add core/db.py tests/test_kakao_extract_db.py
git commit -m "feat: persist kakao extract batch state"
```

---

### Task 5: Orchestrate legacy and Kakao providers through one service

**Files:**
- Modify: `core/extract_link_service.py`
- Create: `tests/test_extract_link_provider_service.py`

- [ ] **Step 1: Write failing service tests**

Cover provider normalization, default lookup, legacy compatibility, Kakao batch scheduling, busy/skipped reporting, partial success, queue-slot release, and restart resumption. The desired bulk entry point is:

```python
result = enqueue_accounts_extract(
    accounts=[
        {"id": 1, "email": "a@example.com", "access_token": "TOKEN_A"},
        {"id": 2, "email": "b@example.com", "access_token": "TOKEN_B"},
    ],
    trigger="manual_bulk",
    provider="kakao_batch",
    batch_size=5,
)

self.assertEqual(result["provider"], "kakao_batch")
self.assertEqual(result["batch_count"], 1)
self.assertEqual(result["started_count"], 2)
```

Use a synchronous fake executor and fake Kakao client so the test verifies real orchestration without network access.

- [ ] **Step 2: Run service tests and verify RED**

```powershell
python -m pytest tests/test_extract_link_provider_service.py -q
```

Expected: failure because the provider-aware bulk entry point does not exist.

- [ ] **Step 3: Refactor without changing legacy behavior**

Keep the existing legacy functions, rename only private helpers when needed, and add:

```python
SUPPORTED_PROVIDERS = {"legacy", "kakao_batch"}


def provider_name(value: str | None = None) -> str:
    ...


def kakao_batch_size(value: int | str | None = None) -> int:
    ...


def enqueue_accounts_extract(
    *,
    accounts: list[dict],
    trigger: str = "manual_bulk",
    provider: str | None = None,
    batch_size: int | str | None = None,
    link_type: str | None = None,
    cdk: str | None = None,
) -> dict:
    ...
```

For `legacy`, call the current per-account `enqueue_account_extract` path and preserve current response semantics. For `kakao_batch`, build batches, claim every account with its result index, submit one future per batch, and update accounts individually from `map_kakao_results`.

Add `resume_interrupted_kakao_batches(recovery)` to submit polling-only futures for persisted `batchId` groups. Recovery must not POST a second batch.

- [ ] **Step 4: Run service and legacy regression tests**

```powershell
python -m pytest tests/test_extract_link_provider_service.py tests/test_kakao_extract_link_provider.py tests/test_kakao_extract_db.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit orchestration**

```powershell
git add core/extract_link_service.py tests/test_extract_link_provider_service.py
git commit -m "feat: orchestrate selectable extract providers"
```

---

### Task 6: Extend Web APIs and startup recovery

**Files:**
- Modify: `webui/app.py`
- Create: `tests/test_webui_extract_link_provider.py`

- [ ] **Step 1: Write failing Flask route tests**

Use `create_app(auth_code="test-auth").test_client()` with patched DB and service calls. Require:

- `GET /api/extract-link/options` returns both providers, saved defaults, batch-size range, and latest Kakao remaining count.
- `POST /api/extract-link/defaults` validates `provider` and `batch_size`, writes only `EXTRACT_LINK_PROVIDER` and `KAKAO_EXTRACT_BATCH_SIZE`, and reloads configuration.
- Single and bulk routes pass `provider` and `batch_size` to the service.
- Kakao bulk returns `batch_count`; legacy bulk remains compatible.
- `_compact_account_for_list` exposes provider and non-secret batch status fields but never Access Token or CDK.

- [ ] **Step 2: Run route tests and verify RED**

```powershell
python -m pytest tests/test_webui_extract_link_provider.py -q
```

Expected: 404 or assertion failures for missing endpoints and payload forwarding.

- [ ] **Step 3: Implement provider-aware routes**

Add the two lightweight options/defaults routes. Refactor bulk startup so the route performs eligibility filtering once, then calls `enqueue_accounts_extract` rather than directly looping through the legacy enqueue function.

At `create_app` startup:

```python
recovery = db.recover_interrupted_extract_links()
extract_link_service.resume_interrupted_kakao_batches(recovery.get("kakao_batches") or [])
```

Log failed recoveries and resumed batch counts separately. If resume queueing fails, update affected accounts with a clear retryable failure.

- [ ] **Step 4: Run route tests and verify GREEN**

```powershell
python -m pytest tests/test_webui_extract_link_provider.py tests/test_webui_auth.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Web API support**

```powershell
git add webui/app.py tests/test_webui_extract_link_provider.py
git commit -m "feat: expose extract provider controls"
```

---

### Task 7: Add simple provider controls to the WebUI

**Files:**
- Modify: `webui/templates/index.html`
- Create: `tests/test_webui_extract_link_provider_template.py`

- [ ] **Step 1: Write failing template contract tests**

Read the template as UTF-8 and assert it contains stable control IDs and payload fields:

```python
self.assertIn('id="extractLinkProvider"', self.html)
self.assertIn('id="extractLinkBatchSize"', self.html)
self.assertIn('id="btnSaveExtractDefaults"', self.html)
self.assertIn("provider: currentExtractProvider()", self.html)
self.assertIn("batch_size: currentExtractBatchSize()", self.html)
self.assertIn("/api/extract-link/options", self.html)
self.assertIn("/api/extract-link/defaults", self.html)
```

Also require a dedicated `提链` configuration subsection mapper for “通用配置”“旧接口”“Kakao API”.

- [ ] **Step 2: Run template tests and verify RED**

```powershell
python -m pytest tests/test_webui_extract_link_provider_template.py -q
```

Expected: failures because the controls and scripts are absent.

- [ ] **Step 3: Implement the toolbar controls**

Add compact controls beside the existing buttons:

```html
<label class="inline-num" title="选择本次提链使用的服务">
  服务
  <select id="extractLinkProvider">
    <option value="legacy">旧接口</option>
    <option value="kakao_batch">Kakao API</option>
  </select>
</label>
<label class="inline-num" id="extractLinkBatchSizeWrap" title="Kakao 每批提交数量，范围 1-5">
  每批
  <input id="extractLinkBatchSize" type="number" min="1" max="5" value="5">
</label>
<button class="btn" id="btnSaveExtractDefaults" type="button">设为默认</button>
```

Load options when the account tab loads. Hide or disable the batch-size field for `legacy`. Include provider, valid account count, batch size, and estimated batch count in confirmations. Both the row button and bulk button send the current provider; only Kakao bulk sends the selected batch size.

Update status labels and titles to include the provider without exposing internal IDs unless useful for diagnosis. Keep the single existing green “提链” action instead of adding duplicate provider-specific buttons.

- [ ] **Step 4: Add three simple configuration subsections**

Follow the existing email/SMS/Codex subtab pattern with `CONFIG_EXTRACT_ACTIVE_SECTION` and `extractConfigSectionForKey`. Use these sections:

- `通用配置`
- `旧接口`
- `Kakao API`

Do not add a new configuration page or modal.

- [ ] **Step 5: Run template tests and verify GREEN**

```powershell
python -m pytest tests/test_webui_extract_link_provider_template.py tests/test_webui_account_token_copy_template.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit frontend controls**

```powershell
git add webui/templates/index.html tests/test_webui_extract_link_provider_template.py
git commit -m "feat: add extract provider controls to webui"
```

---

### Task 8: Update documentation and verify the complete feature

**Files:**
- Modify: `README.md`
- Test: all focused tests
- Test: full repository suite

- [ ] **Step 1: Document both providers**

Update the extract-link configuration section with:

```text
EXTRACT_LINK_PROVIDER=legacy
KAKAO_EXTRACT_API_BASE=https://tiqu.dxmcs.xin
KAKAO_EXTRACT_CDK=
KAKAO_EXTRACT_BATCH_SIZE=5
KAKAO_EXTRACT_TIMEOUT_SECONDS=930
KAKAO_EXTRACT_POLL_INTERVAL=4
```

State that WebUI accepts `1～5`, manually selected accounts are the only accounts processed, and Kakao automatically chunks larger selections.

- [ ] **Step 2: Run focused feature tests**

```powershell
python -m pytest tests/test_kakao_extract_config.py tests/test_kakao_extract_link_provider.py tests/test_kakao_extract_db.py tests/test_extract_link_provider_service.py tests/test_webui_extract_link_provider.py tests/test_webui_extract_link_provider_template.py -q
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the complete regression suite**

```powershell
python -m pytest -q
```

Expected: at least the existing baseline of `244 passed, 15 subtests passed`, plus all new tests.

- [ ] **Step 4: Run source checks**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only intended feature files plus the pre-existing untracked `logs/` appear.

- [ ] **Step 5: Restart and inspect WebUI port 5002**

Restart only the existing WebUI process serving `http://127.0.0.1:5002/`, leaving other processes untouched. Verify in Chrome:

- the provider dropdown loads;
- Kakao shows a `1～5` batch-size input;
- legacy hides or disables the batch-size input;
- “设为默认” persists after reload;
- row and bulk confirmation text names the chosen provider;
- no real extraction is submitted without an explicit test CDK request.

- [ ] **Step 6: Commit documentation and any final verified fixes**

```powershell
git add README.md
git commit -m "docs: document kakao extract provider"
```

- [ ] **Step 7: Final branch audit**

```powershell
git status --short --branch
git log --oneline --decorate -10
```

Expected: branch remains `codex/hero-sms-provider`, `logs/` stays untracked, and `main` has not been switched to or merged.
