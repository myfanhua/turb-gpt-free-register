# iCloud Independent Pickup URL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support per-mailbox independent iCloud HTML pickup URLs with tolerant three-or-more-dash imports, deterministic same-mailbox fallback, OTP extraction, and credential-safe UI/log output.

**Architecture:** Keep the existing iCloud pool and `ICloudMailAccount` task context. Add a focused HTML page parser module, store an explicit `pickup_mode` on each mailbox, and make `fetch_latest_otp()` build its source order from that mailbox only. Raw credentials remain in internal storage/context while list endpoints expose only mode and boolean capability fields.

**Tech Stack:** Python 3, Flask, `requests`, standard-library `html.parser`, `unittest`/pytest, JSON-backed mailbox pool.

---

### Task 1: Tolerant import parsing and mailbox source metadata

**Files:**
- Modify: `webui/app.py:1225-1292`
- Modify: `core/db.py:106-126,540-550,1692-1752`
- Test: `tests/test_webui_icloud.py`
- Test: `tests/test_icloud_pool.py`

- [ ] **Step 1: Add failing WebUI parser tests**

Add tests covering URL-only imports with three, four, five, and six dashes; a mailbox local-part containing dashes; Token + URL; and preservation of legacy Token separators. Use URLs such as:

```python
"dash-name@icloud.com---https://pickup.example/show/credential/dash-name@icloud.com"
"four@icloud.com----https://pickup.example/show/credential/four@icloud.com"
"five@icloud.com-----https://pickup.example/show/credential/five@icloud.com"
"six@icloud.com------https://pickup.example/show/credential/six@icloud.com"
"mixed@icloud.com----tok_mixed----https://pickup.example/show/credential/mixed@icloud.com"
```

Assert that `db.import_icloud_emails()` receives `token=""` for URL-only rows and receives the complete URL without splitting its path.

- [ ] **Step 2: Run the WebUI tests and confirm the current parser fails**

Run:

```powershell
python -m pytest tests/test_webui_icloud.py -q
```

Expected: the new URL-only and variable-dash assertions fail because the second field is currently treated as a Token.

- [ ] **Step 3: Implement a dedicated iCloud line parser**

Add `_parse_icloud_import_line(line: str) -> dict` near the import route in `webui/app.py`.

Implementation rules:

```python
_ICLOUD_DASH_IMPORT_RE = re.compile(
    r"^(?P<email>[^\s@]+@icloud\.com)-{3,}(?P<material>.+)$",
    re.IGNORECASE,
)
_ICLOUD_URL_SPLIT_RE = re.compile(r"-{3,}(?=https://)", re.IGNORECASE)
```

- If `material.startswith("https://")`, return `{"email": email, "token": "", "pickup_url": material}`.
- Otherwise split `material` once with `_ICLOUD_URL_SPLIT_RE`; the first item is Token and an optional second item is URL.
- If the anchored dash format does not match, execute the existing legacy separator logic.
- Keep malformed rows in `records` so the DB layer can count them as `invalid`.

Replace the inline parser in `api_outlook_import()` with this helper.

- [ ] **Step 4: Add failing pool tests for URL-only records, mode derivation, mismatch rejection, and redaction**

Add assertions equivalent to:

```python
result = db.import_icloud_emails([{
    "email": "one@icloud.com",
    "token": "",
    "pickup_url": "https://pickup.example/show/secret/one@icloud.com",
}])
self.assertEqual(result["inserted"], 1)

public_row = db.get_icloud_email_by_email("one@icloud.com")
self.assertEqual(public_row["pickup_mode"], "independent_url")
self.assertTrue(public_row["has_pickup_url"])
self.assertNotIn("pickup_url", public_row)
self.assertNotIn("secret", str(public_row))

claimed = db.claim_next_icloud_email()
self.assertEqual(claimed["pickup_url"], "https://pickup.example/show/secret/one@icloud.com")
```

Also test `api_token`, `independent_url_with_token`, URL-path mailbox mismatch, and update-in-place for duplicate emails.

- [ ] **Step 5: Run the pool tests and confirm they fail**

Run:

```powershell
python -m pytest tests/test_icloud_pool.py -q
```

Expected: URL-only import is invalid, `pickup_mode`/`has_pickup_url` are absent, and public rows still contain the raw URL.

- [ ] **Step 6: Implement DB validation, mode derivation, and public decoration**

In `core/db.py` add:

```python
def _icloud_pickup_mode(token: str, pickup_url: str) -> str:
    if pickup_url and token:
        return "independent_url_with_token"
    if pickup_url:
        return "independent_url"
    return "api_token"

def _valid_icloud_pickup_url(email: str, pickup_url: str) -> bool:
    parsed = urlparse(pickup_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    tail = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]).lower()
    return "@" not in tail or tail == email.lower()
```

Update `import_icloud_emails()` to accept Token or URL, reject invalid/mismatched URLs, store `pickup_mode`, and replace both Token and URL when updating an existing non-running record. Derive missing legacy modes when decorating/claiming.

Update `_decorate_icloud_email()` so normal callers receive:

```python
out.pop("pickup_url", None)
out["pickup_mode"] = _icloud_pickup_mode(token, pickup_url)
out["has_pickup_url"] = bool(pickup_url)
out["copy_line"] = out.get("email") or ""
```

When `include_token=True`, return both raw Token and raw URL because the claim/context path requires them.

- [ ] **Step 7: Run focused tests and commit Task 1**

Run:

```powershell
python -m pytest tests/test_webui_icloud.py tests/test_icloud_pool.py -q
```

Expected: all tests pass.

Commit:

```powershell
git add webui/app.py core/db.py tests/test_webui_icloud.py tests/test_icloud_pool.py
git commit -m "feat: import independent icloud pickup urls"
```

### Task 2: HTML pickup page parser

**Files:**
- Create: `core/icloud_pickup_page.py`
- Create: `tests/test_icloud_pickup_page.py`

- [ ] **Step 1: Write failing parser tests using synthetic HTML fixtures**

Cover the observed page structure:

```html
<div class="cnt">2 封</div>
<div class="card">
  <div class="fr">ChatGPT &lt;noreply@tm.openai.com&gt;</div>
  <div class="su">Your verification code is 654321</div>
  <div class="dt">2026-08-02 14:00:00</div>
  <div class="bd"><p>Use code <strong>654321</strong></p></div>
</div>
```

Tests must cover:

- `with_message_limit()` adds `n=10` and preserves existing query parameters.
- The observed empty page (`<div class="cnt">0 封</div><div class="no">...`) returns an empty list.
- Nested tags and HTML entities are converted to readable text.
- Multiple `.card` entries retain separate sender, subject, date, and body fields.
- A malformed page with neither `.cnt`, `.no`, nor `.card` raises `ICloudPickupPageError`.

- [ ] **Step 2: Run the new tests and confirm the module is missing**

Run:

```powershell
python -m pytest tests/test_icloud_pickup_page.py -q
```

Expected: collection/import failure for `core.icloud_pickup_page`.

- [ ] **Step 3: Implement the parser with the standard library**

Create:

```python
class ICloudPickupPageError(ValueError):
    pass

def with_message_limit(url: str, limit: int = 10) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["n"] = str(limit)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))

def parse_pickup_page(html_text: str) -> list[dict]:
    parser = _PickupPageParser()
    parser.feed(html_text or "")
    parser.close()
    if parser.cards:
        return parser.cards
    if parser.saw_empty_state or parser.message_count == 0:
        return []
    raise ICloudPickupPageError("独立取件页面结构无法识别")
```

Implement `_PickupPageParser(HTMLParser)` with a stack of active CSS classes. Start a message on `.card`; append decoded text inside `.fr`, `.su`, `.dt`, and `.bd`; finalize the card at the matching closing tag. Normalize whitespace without merging separate cards.

- [ ] **Step 4: Run parser tests and commit Task 2**

Run:

```powershell
python -m pytest tests/test_icloud_pickup_page.py -q
```

Expected: all tests pass.

Commit:

```powershell
git add core/icloud_pickup_page.py tests/test_icloud_pickup_page.py
git commit -m "feat: parse independent icloud pickup pages"
```

### Task 3: Deterministic source selection and OTP polling

**Files:**
- Modify: `core/icloud_mail_client.py:28-33,66-104,116-198,317-430`
- Test: `tests/test_icloud_mail_client.py`

- [ ] **Step 1: Add failing source-isolation and HTML OTP tests**

Add tests for:

1. URL-only account requests only its independent URL and does not call Profile.
2. The request URL contains `n=10`, while logs/errors do not contain the credential path.
3. Empty HTML followed by an OTP HTML page returns the new code.
4. Multiple messages skip unrelated senders and old timestamps.
5. Mixed mode tries HTML first, then the same account's JSON Token, then Profile.
6. Two mailbox contexts use two different independent URLs.
7. A malformed HTML page produces a redacted error.

Use `ICloudMailAccount(email=..., token=..., pickup_url=..., pickup_mode=...)` and mock `requests.get`/`requests.post` so no live upstream is contacted.

- [ ] **Step 2: Run the focused client tests and confirm they fail**

Run:

```powershell
python -m pytest tests/test_icloud_mail_client.py -q
```

Expected: `pickup_mode` is unsupported and HTML responses are sent through the JSON parser.

- [ ] **Step 3: Extend account context and add credential redaction**

Add `pickup_mode: str = "api_token"` to `ICloudMailAccount` and populate it in both `pick_account()` and `get_account_context()`.

Add helpers:

```python
def _redact_account_secrets(text: object, account: ICloudMailAccount) -> str:
    value = str(text or "")
    for secret in (account.token, account.pickup_url):
        if secret:
            value = value.replace(secret, "***")
    parsed = urlsplit(account.pickup_url) if account.pickup_url else None
    if parsed and parsed.path:
        value = value.replace(parsed.path, "/***")
    return value

def _account_sources(account: ICloudMailAccount) -> list[tuple[str, Callable]]:
    # independent_url: HTML only
    # independent_url_with_token: HTML, JSON Pickup, optional Profile
    # api_token: JSON Pickup, optional Profile
```

Keep `_api_url()` for JSON endpoints only. Add `_request_independent_page(account)` that calls `with_message_limit(account.pickup_url, 10)` with `Accept: text/html` and no Authorization header.

- [ ] **Step 4: Integrate HTML messages with existing OTP validation**

Convert each parsed card to the existing message shape:

```python
message = {
    "to": account.email,
    "from": card.get("from", ""),
    "subject": card.get("subject", ""),
    "date": card.get("date", ""),
    "text": card.get("body", ""),
    "html": "",
}
```

The page URL itself is mailbox-specific, so the adapter supplies `to=account.email`; all other checks remain unchanged. Sort valid cards by timestamp descending, reject cards older than `after_ts - 30`, require `looks_like_openai_email(message)`, and extract with `extract_otp(message)`.

Refactor the polling loop so each source declares its response type (`html`, `pickup_json`, `profile_json`). Preserve existing 401/403, 404, 429, 5xx, provider-outage, polling, and settle behavior. Only add Profile when the mailbox mode has a Token and a Profile Token is configured.

- [ ] **Step 5: Run client and existing provider tests**

Run:

```powershell
python -m pytest tests/test_icloud_pickup_page.py tests/test_icloud_mail_client.py tests/test_icloud_email_provider.py -q
```

Expected: all tests pass, including all previous Pickup/Profile behavior.

- [ ] **Step 6: Commit Task 3**

```powershell
git add core/icloud_mail_client.py tests/test_icloud_mail_client.py
git commit -m "feat: read otp from independent icloud pages"
```

### Task 4: UI labels, leak checks, and full regression

**Files:**
- Modify: `webui/templates/index.html:576-593,2253-2288`
- Modify: `tests/test_webui_icloud_template.py`
- Modify: `tests/test_webui_icloud.py`

- [ ] **Step 1: Add failing UI/template tests**

Assert that the template contains:

```text
邮箱 + Token
邮箱 + 独立取件 URL
三个及以上横线
```

Assert that the rendered pool code uses `pickup_mode` to display `API Token`, `独立 URL`, or `URL + API 后备`, and never renders `pickup_url`.

- [ ] **Step 2: Update the import help and pool credential preview**

Change the iCloud help/placeholder to show Token-only and URL-only formats. Add a JavaScript helper:

```javascript
function icloudPickupLabel(mode) {
  return ({
    api_token: 'API Token',
    independent_url: '独立 URL',
    independent_url_with_token: 'URL + API 后备'
  })[mode] || '已配置';
}
```

Use the label plus masked Token where applicable; never interpolate a raw URL.

- [ ] **Step 3: Run all focused tests**

Run:

```powershell
python -m pytest tests/test_webui_icloud.py tests/test_webui_icloud_template.py tests/test_icloud_pool.py tests/test_icloud_pickup_page.py tests/test_icloud_mail_client.py tests/test_icloud_email_provider.py tests/test_sms_provider_sms_activate.py -q
```

Expected: all focused tests pass, including the existing HeroSMS provider and WebUI coverage.

- [ ] **Step 4: Scan tracked changes for the real credential and secret-bearing URLs**

Run:

```powershell
git diff --check
git diff --cached --check
git grep -n "icloud-api.top/show/" -- ':!docs/superpowers/specs/*' ':!docs/superpowers/plans/*'
git status --short
```

Expected: no real independent URL appears in tracked code/tests/docs; only `logs/` remains untracked outside the intended changes.

- [ ] **Step 5: Run the complete regression suite**

Run:

```powershell
python -m pytest -q
```

Expected: the full suite passes with no regressions.

- [ ] **Step 6: Commit the UI and final verification changes**

```powershell
git add webui/templates/index.html tests/test_webui_icloud_template.py tests/test_webui_icloud.py
git commit -m "feat: show icloud pickup source modes"
```

- [ ] **Step 7: Verify final branch state**

Run:

```powershell
git status --short --branch
git log -6 --oneline
```

Expected: branch `codex/hero-sms-provider`, intended commits present, and only the pre-existing untracked `logs/` directory remains.
