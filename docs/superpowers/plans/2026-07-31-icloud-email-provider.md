# iCloud Pickup Email Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `icloud_api` email source that batch-imports mailbox/token pairs, atomically assigns one pair per registration task, and reads only the OTP belonging to that task.

**Architecture:** Store iCloud mailbox credentials in a dedicated JSON-backed pool guarded by the existing `core.db` lock. A focused `core.icloud_mail_client` owns HTTP polling and response identity checks, while `core.email_provider` routes acquisition, OTP lookup, release, and unconsumed recovery. Existing WebUI pool routes and the single-page frontend gain `icloud_api` as another concrete pool source without exposing raw tokens.

**Tech Stack:** Python 3, Flask, `requests`, JSON file persistence, `unittest`, `unittest.mock`, native HTML/JavaScript.

---

## File map

- Create `core/icloud_mail_client.py`: mailbox/token context, pickup API polling, response validation, OTP extraction, and status-aware retries.
- Create `tests/test_icloud_pool.py`: persistence, import/update rules, masking, recovery, and concurrent claiming.
- Create `tests/test_icloud_mail_client.py`: headers, mailbox binding, timestamps, OTP extraction, and HTTP status handling.
- Create `tests/test_icloud_email_provider.py`: source parsing and routing.
- Create `tests/test_webui_icloud.py`: WebUI import/list/status/delete/job-capacity behavior.
- Create `tests/test_webui_icloud_template.py`: static frontend contract assertions.
- Modify `core/db.py`: dedicated iCloud pool storage and atomic operations.
- Modify `core/email_provider.py`: add `icloud_api` routing.
- Modify `config/email.py`: API base and timeout configuration.
- Modify `webui/config_editor.py`: editable iCloud API configuration.
- Modify `webui/app.py`: pool endpoints, token-safe serialization, and registration capacity warnings.
- Modify `webui/templates/index.html`: import options, labels, masked token display, and result counts.
- Modify `.env.example`, `.gitignore`, and `README.md`: configuration and usage documentation.

---

### Task 1: Configuration and source declaration

**Files:**
- Create: `tests/test_icloud_config.py`
- Modify: `config/email.py`
- Modify: `webui/config_editor.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_icloud_config.py`:

```python
# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from webui.config_editor import EDITABLE_FIELDS


class ICloudConfigTests(unittest.TestCase):
    def test_email_config_declares_pickup_defaults(self):
        self.assertEqual(
            email.ICLOUD_PICKUP_API_BASE,
            "https://icloud.flysms.top/icloud/api/pickup",
        )
        self.assertEqual(email.ICLOUD_PICKUP_TIMEOUT, 15)

    def test_email_config_env_override_registry_contains_pickup_fields(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn("'ICLOUD_PICKUP_API_BASE': 'str'", source)
        self.assertIn("'ICLOUD_PICKUP_TIMEOUT': 'int'", source)

    def test_webui_exposes_pickup_base_and_timeout(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["ICLOUD_PICKUP_API_BASE"]["group"], "邮箱 / OTP")
        self.assertEqual(fields["ICLOUD_PICKUP_TIMEOUT"]["type"], "int")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and observe the missing attributes**

Run:

```powershell
python -m unittest tests.test_icloud_config -v
```

Expected: failures mentioning missing `ICLOUD_PICKUP_API_BASE`, `ICLOUD_PICKUP_TIMEOUT`, or editable fields.

- [ ] **Step 3: Add configuration defaults and editable fields**

Add to `config/email.py` near the other provider configuration blocks:

```python
# ============================================================
# iCloud Pickup 邮箱 API
# ============================================================

ICLOUD_PICKUP_API_BASE = env_str(
    "ICLOUD_PICKUP_API_BASE",
    "https://icloud.flysms.top/icloud/api/pickup",
)
ICLOUD_PICKUP_TIMEOUT = env_int("ICLOUD_PICKUP_TIMEOUT", 15)
```

Add both keys to the existing `apply_env_overrides` mapping:

```python
'ICLOUD_PICKUP_API_BASE': 'str',
'ICLOUD_PICKUP_TIMEOUT': 'int',
```

Update the `EMAIL_SOURCE` comment to include `icloud_api`.

Add to `webui/config_editor.py` immediately after the general email fields:

```python
{
    "key": "ICLOUD_PICKUP_API_BASE",
    "file": "email.py",
    "type": "str",
    "group": "邮箱 / OTP",
    "label": "iCloud Pickup API 地址",
    "help": "icloud_api 邮箱来源使用；默认指向 iCloud Mail Search 的只读取件 API",
},
{
    "key": "ICLOUD_PICKUP_TIMEOUT",
    "file": "email.py",
    "type": "int",
    "group": "邮箱 / OTP",
    "label": "iCloud Pickup 请求超时(秒)",
    "help": "单次读取最新邮件请求的超时时间",
},
```

Add to `.env.example` under the email provider section:

```dotenv
# iCloud Pickup 邮箱 API；EMAIL_SOURCE 含 icloud_api 时使用
ICLOUD_PICKUP_API_BASE=https://icloud.flysms.top/icloud/api/pickup
ICLOUD_PICKUP_TIMEOUT=15
```

- [ ] **Step 4: Run configuration tests**

Run:

```powershell
python -m unittest tests.test_icloud_config -v
```

Expected: all three tests pass.

- [ ] **Step 5: Commit configuration support**

```powershell
git add tests/test_icloud_config.py config/email.py webui/config_editor.py .env.example
git commit -m "feat: add icloud pickup configuration"
```

---

### Task 2: Dedicated token-safe iCloud mailbox pool

**Files:**
- Create: `tests/test_icloud_pool.py`
- Modify: `core/db.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing pool tests**

Create `tests/test_icloud_pool.py`:

```python
# -*- coding: utf-8 -*-
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core import db


class ICloudPoolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.patchers = [
            patch.object(db, "_ICLOUD_EMAIL_JSON", root / "icloud.json"),
            patch.object(db, "_ICLOUD_EMAIL_TXT", root / "icloud.txt"),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
            patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def test_import_inserts_updates_and_masks_tokens(self):
        first = db.import_icloud_emails([
            {"email": "One@icloud.com", "token": "tok_first_1234"},
            {"email": "two@icloud.com", "token": "tok_second_5678"},
        ])
        second = db.import_icloud_emails([
            {"email": "one@icloud.com", "token": "tok_new_9999"},
            {"email": "broken@icloud.com", "token": ""},
        ])

        self.assertEqual(first, {"inserted": 2, "updated": 0, "skipped": 0, "invalid": 0})
        self.assertEqual(second, {"inserted": 0, "updated": 1, "skipped": 0, "invalid": 1})
        rows = db.list_icloud_email_pool()
        one = next(row for row in rows if row["email"] == "one@icloud.com")
        self.assertNotIn("token", one)
        self.assertEqual(one["token_masked"], "tok_****9999")
        self.assertNotIn("tok_new_9999", str(rows))

    def test_concurrent_claims_return_unique_mailboxes(self):
        db.import_icloud_emails([
            {"email": f"mail{i}@icloud.com", "token": f"tok_{i:04d}"}
            for i in range(8)
        ])

        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda _: db.claim_next_icloud_email(), range(8)))

        emails = [row["email"] for row in claimed]
        self.assertEqual(len(set(emails)), 8)
        self.assertEqual(db.icloud_email_pool_summary()["used"], 8)

    def test_unconsumed_used_mailbox_returns_to_available(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()
        self.assertTrue(db.release_unconsumed_icloud_email("one@icloud.com", note="retry"))
        row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(row["status"], "available")
        self.assertIsNone(row["used_at"])

    def test_used_mailbox_cannot_be_deleted(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()
        self.assertFalse(db.delete_icloud_email("one@icloud.com"))

    def test_insert_account_marks_icloud_mailbox_registered_without_copying_token(self):
        db.import_icloud_emails([{"email": "one@icloud.com", "token": "tok_one_1234"}])
        db.claim_next_icloud_email()
        db.insert_account(email="one@icloud.com", access_token="access-123", email_source="icloud_api")
        pool_row = db.get_icloud_email_by_email("one@icloud.com", include_token=True)
        self.assertEqual(pool_row["status"], "registered")
        account = db.get_account_by_email("one@icloud.com")
        self.assertNotIn("tok_one_1234", str(account))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run pool tests and observe missing pool functions**

```powershell
python -m unittest tests.test_icloud_pool -v
```

Expected: errors for missing iCloud storage constants and functions.

- [ ] **Step 3: Add storage constants, masking helpers, and load/save functions**

Add near the existing pool constants in `core/db.py`:

```python
_ICLOUD_EMAIL_JSON = _PROJECT_ROOT / "用于注册的iCloud邮箱.json"
_ICLOUD_EMAIL_TXT = _PROJECT_ROOT / "用于注册的iCloud邮箱.txt"
```

Add focused helpers:

```python
def _icloud_email_line(row: dict) -> str:
    return "----".join([row.get("email") or "", row.get("token") or ""])


def _mask_icloud_token(token: str) -> str:
    value = str(token or "")
    if not value:
        return ""
    suffix = value[-4:] if len(value) >= 4 else value
    prefix = "tok_" if value.startswith("tok_") else ""
    return f"{prefix}****{suffix}"


def _sync_icloud_email_txt(rows: list[dict]) -> None:
    available = [row for row in rows if row.get("status") == "available"]
    lines = [_icloud_email_line(row) for row in sorted(available, key=lambda item: int(item.get("id") or 0))]
    _ICLOUD_EMAIL_TXT.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


def _load_icloud_emails() -> list[dict]:
    rows = _read_json(_ICLOUD_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_icloud_emails(rows: list[dict]) -> None:
    _write_json(_ICLOUD_EMAIL_JSON, rows)
    _sync_icloud_email_txt(rows)


def _decorate_icloud_email(row: dict, *, include_token: bool = False) -> dict:
    out = dict(row)
    token = str(out.pop("token", "") or "")
    out["token_masked"] = _mask_icloud_token(token)
    out["copy_line"] = out.get("email") or ""
    if include_token:
        out["token"] = token
    return out
```

- [ ] **Step 4: Add atomic import, claim, release, list, summary, lookup, and delete functions**

Add a new `icloud_api email pool` section in `core/db.py`:

```python
def import_icloud_emails(records: list[dict]) -> dict[str, int]:
    with _LOCK:
        rows = _load_icloud_emails()
        result = {"inserted": 0, "updated": 0, "skipped": 0, "invalid": 0}
        collapsed: dict[str, str] = {}
        for raw in records:
            email = str(raw.get("email") or "").strip().lower()
            token = str(raw.get("token") or "").strip()
            if not email or "@" not in email or not token:
                result["invalid"] += 1
                continue
            if email in collapsed:
                result["skipped"] += 1
            collapsed[email] = token

        for email, token in collapsed.items():
            row = _find_by_email(rows, email)
            now = _now()
            if row is None:
                rows.append({
                    "id": _next_id(rows),
                    "email": email,
                    "token": token,
                    "status": "available",
                    "used_at": None,
                    "note": None,
                    "created_at": now,
                    "updated_at": now,
                })
                result["inserted"] += 1
                continue
            if row.get("token") == token:
                result["skipped"] += 1
                continue
            row["token"] = token
            row["updated_at"] = now
            if row.get("status") in {"available", "failed", "disabled"}:
                row["status"] = "available"
                row["used_at"] = None
                row["note"] = None
            result["updated"] += 1

        _save_icloud_emails(rows)
        return result


def claim_next_icloud_email() -> dict | None:
    with _LOCK:
        rows = sorted(_load_icloud_emails(), key=lambda item: int(item.get("id") or 0))
        row = next((item for item in rows if item.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["updated_at"] = _now()
        row["note"] = None
        _save_icloud_emails(rows)
        return _decorate_icloud_email(row, include_token=True)


def release_icloud_email(email: str, status: str = "available", note: str | None = None) -> None:
    with _LOCK:
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        row["updated_at"] = _now()
        if status == "available":
            row["used_at"] = None
        else:
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_icloud_emails(rows)


def release_unconsumed_icloud_email(email: str, note: str | None = None) -> bool:
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        row["updated_at"] = _now()
        if note is not None:
            row["note"] = note
        _save_icloud_emails(rows)
        return True


def delete_icloud_email(email: str) -> bool:
    with _LOCK:
        rows = _load_icloud_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") == "used":
            return False
        rows.remove(row)
        _save_icloud_emails(rows)
        return True


def list_icloud_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = _load_icloud_emails()
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda item: int(item.get("id") or 0), reverse=True)
        return [_decorate_icloud_email(row) for row in rows[:limit]]


def icloud_email_pool_summary() -> dict:
    with _LOCK:
        result = {"available": 0, "used": 0, "registered": 0, "failed": 0, "disabled": 0}
        for row in _load_icloud_emails():
            status = row.get("status") or "available"
            result[status] = result.get(status, 0) + 1
        result["total"] = sum(result.values())
        return result


def get_icloud_email_by_email(email: str, *, include_token: bool = False) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_icloud_emails(), email)
        return _decorate_icloud_email(row, include_token=include_token) if row else None
```

Extend `insert_account()` so successful registration marks the matching iCloud pool row as consumed without copying the mailbox Token into the registered account:

```python
icloud_rows = _load_icloud_emails()
icloud_row = _find_by_email(icloud_rows, email)
```

After the existing Outlook row update:

```python
if icloud_row:
    icloud_row["status"] = "registered"
    icloud_row["used_at"] = icloud_row.get("used_at") or _now()
    icloud_row["registered_account_id"] = row_id
    icloud_row["completed_at"] = _now()
    icloud_row["updated_at"] = _now()
    row["original_email_line"] = email
```

Before returning from `insert_account()`:

```python
_save_icloud_emails(icloud_rows)
```

- [ ] **Step 5: Ignore runtime iCloud credential files**

Add to `.gitignore` beside the existing email pools:

```gitignore
用于注册的iCloud邮箱.json
用于注册的iCloud邮箱.txt
```

- [ ] **Step 6: Run pool tests**

```powershell
python -m unittest tests.test_icloud_pool -v
```

Expected: all pool tests pass, including eight unique concurrent claims.

- [ ] **Step 7: Commit the pool**

```powershell
git add tests/test_icloud_pool.py core/db.py .gitignore
git commit -m "feat: add atomic icloud mailbox pool"
```

---

### Task 3: Pickup API client with mailbox identity enforcement

**Files:**
- Create: `tests/test_icloud_mail_client.py`
- Create: `core/icloud_mail_client.py`

- [ ] **Step 1: Write failing client tests**

Create `tests/test_icloud_mail_client.py` with these concrete cases:

```python
# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import icloud_mail_client as client


class ICloudMailClientTests(unittest.TestCase):
    def setUp(self):
        client._CONTEXT_CACHE.clear()
        client._CONTEXT_CACHE["one@icloud.com"] = client.ICloudMailAccount(
            email="one@icloud.com",
            token="tok_one_1234",
        )

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_sends_mailbox_specific_headers(self, get):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {
            "email": "one@icloud.com",
            "message": {
                "uid": 7,
                "to": "one@icloud.com",
                "date": "2026-07-31T03:10:00.000Z",
                "from": "noreply@openai.com",
                "subject": "Your verification code is 654321",
                "text": "Your code is 654321",
                "html": "",
            },
        }
        get.return_value = response

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=1785467390,
            max_wait=1,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "654321")
        get.assert_called_once_with(
            "https://icloud.flysms.top/icloud/api/pickup/messages/latest",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer tok_one_1234",
                "X-Mailbox-Email": "one@icloud.com",
                "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
            },
            timeout=15,
        )

    @patch("core.icloud_mail_client.requests.get")
    def test_fetch_rejects_response_for_another_mailbox(self, get):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {
            "email": "two@icloud.com",
            "message": {
                "uid": 9,
                "to": "two@icloud.com",
                "date": "2026-07-31T03:10:00.000Z",
                "subject": "Code 111111",
                "text": "Code 111111",
            },
        }
        get.return_value = response

        with self.assertRaisesRegex(client.ICloudMailError, "响应邮箱不匹配"):
            client.fetch_latest_otp(
                "one@icloud.com",
                after_ts=1785467390,
                max_wait=0,
                poll_interval=1,
                settle_seconds=0,
            )

    @patch("core.icloud_mail_client.db.release_icloud_email")
    @patch("core.icloud_mail_client.requests.get")
    def test_401_disables_mailbox_without_logging_token(self, get, release):
        response = Mock(status_code=401, headers={})
        response.json.return_value = {
            "error": "Invalid pickup credentials",
            "code": "INVALID_PICKUP_CREDENTIALS",
        }
        get.return_value = response

        with self.assertRaisesRegex(client.ICloudMailError, "401") as raised:
            client.fetch_latest_otp(
                "one@icloud.com",
                after_ts=0,
                max_wait=0,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertNotIn("tok_one_1234", str(raised.exception))
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["status"], "disabled")

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_404_then_new_message_returns_otp(self, get, sleep):
        empty = Mock(status_code=404, headers={})
        empty.json.return_value = {"error": "No messages"}
        found = Mock(status_code=200, headers={})
        found.json.return_value = {
            "email": "one@icloud.com",
            "message": {
                "uid": 10,
                "to": ["one@icloud.com"],
                "date": "2026-07-31T03:10:00.000Z",
                "from": "noreply@openai.com",
                "subject": "OpenAI code 222222",
                "text": "OpenAI code 222222",
            },
        }
        get.side_effect = [empty, found]

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=1785467390,
            max_wait=5,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "222222")
        self.assertEqual(sleep.call_count, 1)

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_429_uses_retry_after_before_retrying(self, get, sleep):
        limited = Mock(status_code=429, headers={"Retry-After": "4"})
        limited.json.return_value = {"error": "Too many requests"}
        found = Mock(status_code=200, headers={})
        found.json.return_value = {
            "email": "one@icloud.com",
            "message": {
                "uid": 11,
                "to": "one@icloud.com",
                "date": "2026-07-31T03:10:00.000Z",
                "from": "noreply@openai.com",
                "subject": "OpenAI code 333333",
                "text": "OpenAI code 333333",
            },
        }
        get.side_effect = [limited, found]

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=1785467390,
            max_wait=5,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "333333")
        self.assertGreaterEqual(sleep.call_args.args[0], 3.0)

    @patch("core.icloud_mail_client.time.sleep")
    @patch("core.icloud_mail_client.requests.get")
    def test_503_is_retried(self, get, sleep):
        unavailable = Mock(status_code=503, headers={})
        unavailable.json.return_value = {"error": "Initializing"}
        found = Mock(status_code=200, headers={})
        found.json.return_value = {
            "email": "one@icloud.com",
            "message": {
                "uid": 12,
                "to": "one@icloud.com",
                "date": "2026-07-31T03:10:00.000Z",
                "from": "noreply@openai.com",
                "subject": "OpenAI code 444444",
                "text": "OpenAI code 444444",
            },
        }
        get.side_effect = [unavailable, found]

        code = client.fetch_latest_otp(
            "one@icloud.com",
            after_ts=1785467390,
            max_wait=5,
            poll_interval=1,
            settle_seconds=0,
        )

        self.assertEqual(code, "444444")
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run client tests and observe the missing module**

```powershell
python -m unittest tests.test_icloud_mail_client -v
```

Expected: import failure for `core.icloud_mail_client`.

- [ ] **Step 3: Implement account acquisition and context lookup**

Create `core/icloud_mail_client.py` with these public definitions:

```python
# -*- coding: utf-8 -*-
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from config import email as _email_cfg
from core import db
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)


class ICloudMailError(RuntimeError):
    pass


@dataclass(frozen=True)
class ICloudMailAccount:
    email: str
    token: str


_CONTEXT_CACHE: dict[str, ICloudMailAccount] = {}


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def pick_account() -> ICloudMailAccount:
    row = db.claim_next_icloud_email()
    if row is None:
        raise ICloudMailError(f"iCloud 邮箱池没有可用邮箱: {db.icloud_email_pool_summary()}")
    account = ICloudMailAccount(email=row["email"], token=row["token"])
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info("[iCloud] 已领取邮箱: %s（DB id=%s）", account.email, row.get("id"))
    return account


def get_account_context(email: str) -> ICloudMailAccount | None:
    key = _cache_key(email)
    cached = _CONTEXT_CACHE.get(key)
    if cached is not None:
        return cached
    row = db.get_icloud_email_by_email(key, include_token=True)
    if row is None:
        return None
    account = ICloudMailAccount(email=row["email"], token=row["token"])
    _CONTEXT_CACHE[key] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    current = db.get_icloud_email_by_email(email)
    if current and current.get("status") == "disabled" and status == "available":
        status = "disabled"
    db.release_icloud_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(_cache_key(email), None)
```

This guard prevents an outer registration exception handler from changing a credential-disabled mailbox back to `available`. WebUI manual recovery calls `db.release_icloud_email` directly and remains able to restore the row.

- [ ] **Step 4: Implement response identity, date parsing, and polling**

Add the following helpers and `fetch_latest_otp` to `core/icloud_mail_client.py`:

```python
def _api_url() -> str:
    base = str(getattr(_email_cfg, "ICLOUD_PICKUP_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ICloudMailError("请填写 iCloud Pickup API 地址")
    return f"{base}/messages/latest"


def _message_timestamp(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 1e12 else value
    text = str(raw).strip()
    try:
        value = float(text)
        return value / 1000.0 if value > 1e12 else value
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _recipient_matches(value, target: str) -> bool:
    target = _cache_key(target)
    if isinstance(value, str):
        return target in value.lower()
    if isinstance(value, list):
        return any(_recipient_matches(item, target) for item in value)
    if isinstance(value, dict):
        return any(_recipient_matches(item, target) for item in value.values())
    return False


def _response_message(payload: object, target: str, after_ts: float | None) -> tuple[dict, float, str]:
    if not isinstance(payload, dict):
        raise ICloudMailError("iCloud Pickup 响应不是 JSON 对象")
    response_email = _cache_key(payload.get("email"))
    if response_email != _cache_key(target):
        raise ICloudMailError(f"iCloud Pickup 响应邮箱不匹配: expected={target}, actual={response_email or '-'}")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ICloudMailError("iCloud Pickup 响应缺少 message 对象")
    if not _recipient_matches(message.get("to"), target):
        raise ICloudMailError(f"iCloud Pickup 收件人不匹配: expected={target}")
    stamp = _message_timestamp(message.get("date"))
    if stamp is None:
        raise ICloudMailError(f"iCloud Pickup 邮件时间无效: email={target}")
    if after_ts is not None and stamp < float(after_ts) - 30:
        return message, stamp, "old"
    return message, stamp, "new"


def _request_latest(account: ICloudMailAccount):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {account.token}",
        "X-Mailbox-Email": account.email,
        "User-Agent": "Mozilla/5.0 (compatible; turb-gpt-register/1.0)",
    }
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_PICKUP_TIMEOUT", 15) or 15))
    return requests.get(_api_url(), headers=headers, timeout=timeout)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    account = get_account_context(email)
    if account is None:
        raise ICloudMailError(f"iCloud 邮箱不存在或未领取: {email}")
    wait_seconds = max(0, int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT))
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + wait_seconds
    best_otp: str | None = None
    best_key = ""
    settle_until: float | None = None
    last_error = "尚未出现新的 OpenAI 验证码"

    while time.monotonic() <= deadline:
        try:
            response = _request_latest(account)
            status = int(response.status_code)
            if status in {401, 403}:
                db.release_icloud_email(
                    account.email,
                    status="disabled",
                    note=f"iCloud Pickup HTTP {status}",
                )
                raise ICloudMailError(f"iCloud Pickup HTTP {status}: 邮箱凭据无效、到期或停用")
            if status == 404:
                last_error = "当前邮箱没有可读取的邮件"
            elif status == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    interval = max(interval, min(30, int(float(retry_after))))
                except (TypeError, ValueError):
                    interval = max(interval, 3)
                last_error = "请求过于频繁"
            elif status == 503:
                last_error = "邮箱正在初始化或暂时无法刷新"
            elif status >= 500:
                last_error = f"服务暂时异常: HTTP {status}"
            elif status != 200:
                raise ICloudMailError(f"iCloud Pickup HTTP {status}: email={account.email}")
            else:
                message, stamp, freshness = _response_message(response.json(), account.email, after_ts)
                if freshness == "old":
                    last_error = "最新邮件早于本次验证码请求"
                elif not looks_like_openai_email(message):
                    last_error = "最新邮件不是 OpenAI 验证邮件"
                else:
                    code = extract_otp(message)
                    if code:
                        key = f"{message.get('uid') or ''}:{stamp}:{code}"
                        if key != best_key:
                            best_key = key
                            best_otp = code
                            settle_until = time.monotonic() + settle
        except ICloudMailError:
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        now = time.monotonic()
        if best_otp and settle_until is not None and now >= settle_until:
            return best_otp
        if now >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - now)))

    if best_otp:
        return best_otp
    raise ICloudMailError(f"等待 iCloud 验证码超时: {account.email}; {last_error}")
```

- [ ] **Step 5: Run client tests**

```powershell
python -m unittest tests.test_icloud_mail_client -v
```

Expected: all client tests pass and no exception text contains a raw Token.

- [ ] **Step 6: Commit the client**

```powershell
git add tests/test_icloud_mail_client.py core/icloud_mail_client.py
git commit -m "feat: add isolated icloud pickup client"
```

---

### Task 4: Route iCloud through the common email provider

**Files:**
- Create: `tests/test_icloud_email_provider.py`
- Modify: `core/email_provider.py`

- [ ] **Step 1: Write failing routing tests**

Create `tests/test_icloud_email_provider.py`:

```python
# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import email_provider


class ICloudEmailProviderTests(unittest.TestCase):
    def test_parse_email_sources_accepts_icloud_api(self):
        self.assertEqual(
            email_provider.parse_email_sources("icloud_api,outlook,icloud_api"),
            ["icloud_api", "outlook"],
        )

    @patch("core.icloud_mail_client.pick_account")
    def test_acquire_email_uses_icloud_client(self, pick_account):
        pick_account.return_value.email = "one@icloud.com"
        with patch("core.email_provider.parse_email_sources", return_value=["icloud_api"]):
            self.assertEqual(email_provider.acquire_email(), "one@icloud.com")

    @patch("core.icloud_mail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="icloud_api")
    def test_wait_for_otp_routes_explicit_email(self, resolve, fetch):
        code = email_provider.wait_for_otp("one@icloud.com", after_ts=100, max_wait=10)
        self.assertEqual(code, "654321")
        fetch.assert_called_once_with("one@icloud.com", after_ts=100, max_wait=10)

    @patch("core.db.release_unconsumed_icloud_email", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="icloud_api")
    def test_release_unconsumed_routes_to_icloud_pool(self, resolve, release):
        self.assertTrue(email_provider.release_email_if_unconsumed("one@icloud.com", note="retry"))
        release.assert_called_once_with("one@icloud.com", note="retry")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run routing tests and observe failures**

```powershell
python -m unittest tests.test_icloud_email_provider -v
```

Expected: `icloud_api` is filtered out or routed to Outlook.

- [ ] **Step 3: Add explicit `icloud_api` branches**

Modify `core/email_provider.py`:

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
)
```

Add to `_pick_from_source` before the Outlook fallback:

```python
if source == "icloud_api":
    from core.icloud_mail_client import pick_account
    return pick_account().email
```

Add to `resolve_email_source` before Outlook lookup:

```python
if db.get_icloud_email_by_email(email):
    return "icloud_api"
```

Add to `wait_for_otp` before the Outlook fallback:

```python
if source == "icloud_api":
    from core.icloud_mail_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
```

Add to `release_email`:

```python
elif source == "icloud_api":
    from core.icloud_mail_client import release_account
    release_account(email, status=status, note=note)
```

Add to `release_email_if_unconsumed`:

```python
elif source == "icloud_api":
    changed = db.release_unconsumed_icloud_email(email, note=note)
```

- [ ] **Step 4: Run routing and existing provider tests**

```powershell
python -m unittest tests.test_icloud_email_provider tests.test_email_provider_cloudflare tests.test_email_provider_gptmail -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit provider routing**

```powershell
git add tests/test_icloud_email_provider.py core/email_provider.py
git commit -m "feat: route icloud mailboxes through email provider"
```

---

### Task 5: WebUI backend import, management, and capacity checks

**Files:**
- Create: `tests/test_webui_icloud.py`
- Modify: `webui/app.py`

- [ ] **Step 1: Write failing WebUI tests**

Create `tests/test_webui_icloud.py`:

```python
# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class ICloudWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.import_icloud_emails")
    def test_import_accepts_email_token_lines(self, import_icloud):
        import_icloud.return_value = {"inserted": 1, "updated": 1, "skipped": 0, "invalid": 1}
        response = self.client.post("/api/outlook/import", json={
            "source": "icloud_api",
            "text": "one@icloud.com----tok_one\ntwo@icloud.com====tok_two\nbroken",
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["updated"], 1)
        self.assertEqual(payload["invalid"], 1)
        import_icloud.assert_called_once_with([
            {"email": "one@icloud.com", "token": "tok_one"},
            {"email": "two@icloud.com", "token": "tok_two"},
            {"email": "broken", "token": ""},
        ])

    @patch("webui.app.db.list_icloud_email_pool")
    def test_list_icloud_pool_returns_only_masked_token(self, list_pool):
        list_pool.return_value = [{
            "id": 1,
            "email": "one@icloud.com",
            "status": "available",
            "token_masked": "tok_****1234",
        }]
        response = self.client.get("/api/outlook?source=icloud_api")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload[0]["source"], "icloud_api")
        self.assertNotIn("token", payload[0])

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    @patch("webui.app.db.icloud_email_pool_summary", return_value={"available": 1, "total": 1})
    def test_jobs_warn_when_icloud_pool_is_smaller_than_count(self, summary, submit):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "icloud_api"
        ):
            response = self.client.post("/api/jobs", json={"count": 2, "workers": 2})
        self.assertEqual(response.status_code, 200)
        self.assertIn("iCloud 邮箱池仅 1 个可用", response.get_json()["warning"])

    @patch("webui.app.db.delete_icloud_email", return_value=False)
    def test_delete_used_icloud_mailbox_reports_not_deleted(self, delete):
        response = self.client.post("/api/outlook/delete", json={
            "source": "icloud_api",
            "email": "one@icloud.com",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["deleted"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run WebUI tests and observe unsupported source failures**

```powershell
python -m unittest tests.test_webui_icloud -v
```

Expected: import rejects `icloud_api`, listing routes to Outlook, and job capacity uses the wrong pool.

- [ ] **Step 3: Extend pool listing and import routes**

In `webui/app.py`, include iCloud rows in `/api/outlook`:

```python
rows += _with_pool_source(
    db.list_icloud_email_pool(status=status, limit=fetch_limit),
    "icloud_api",
)
```

Add the concrete source branch:

```python
elif source == "icloud_api":
    rows = _with_pool_source(
        db.list_icloud_email_pool(status=status, limit=fetch_limit),
        "icloud_api",
    )
```

Allow `icloud_api` in the import source validation and parse only two fields:

```python
if source not in ("outlook", "generic_api", "icloud_api"):
    return jsonify({"ok": False, "error": "导入时请选择 Outlook、通用 API 或 iCloud API"}), 400
```

```python
if source == "icloud_api":
    records.append({
        "email": parts[0] if parts else "",
        "token": parts[1] if len(parts) > 1 else "",
    })
    continue
```

Call the pool importer and preserve its full counts:

```python
elif source == "icloud_api":
    as_registered = False
    result = db.import_icloud_emails(records)
    return jsonify({
        "ok": True,
        "parsed": len(records),
        "as_registered": False,
        **result,
    })
```

- [ ] **Step 4: Extend status, bulk status, delete, and bulk delete routing**

At each existing pool dispatch point, insert the iCloud branch before the Outlook fallback:

```python
elif source == "icloud_api":
    db.release_icloud_email(email, status=status, note=data.get("note"))
```

```python
elif item_source == "icloud_api":
    db.release_icloud_email(email, status=status, note=note)
```

```python
db.delete_icloud_email(email)
if source == "icloud_api"
else db.delete_generic_api_email(email)
if source == "generic_api"
else db.delete_domain_email(email)
if source == "cloudflare_domain"
else db.delete_outlook(email)
```

Use the same ordering for the bulk delete expression.

- [ ] **Step 5: Add iCloud capacity accounting to job submission and summary**

Add a single-source branch near `generic_api`:

```python
elif sources == ["icloud_api"]:
    pool = db.icloud_email_pool_summary()
    warning = ""
    if pool.get("available", 0) < count:
        warning = (
            f"iCloud 邮箱池仅 {pool.get('available', 0)} 个可用，"
            f"少于任务数 {count}，不足的会失败"
        )
```

Add to the multi-source total:

```python
if "icloud_api" in sources:
    available += db.icloud_email_pool_summary().get("available", 0)
```

Where `/api/summary` combines local mailbox pools, add the iCloud total and available counts instead of treating `icloud_api` as Outlook.

- [ ] **Step 6: Run WebUI backend tests**

```powershell
python -m unittest tests.test_webui_icloud tests.test_webui_gptmail tests.test_webui_cloudflare -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit backend integration**

```powershell
git add tests/test_webui_icloud.py webui/app.py
git commit -m "feat: manage icloud mailbox pool in webui"
```

---

### Task 6: Frontend import and masked mailbox display

**Files:**
- Create: `tests/test_webui_icloud_template.py`
- Modify: `webui/templates/index.html`

- [ ] **Step 1: Write failing static frontend tests**

Create `tests/test_webui_icloud_template.py`:

```python
# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class ICloudTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_import_and_pool_selects_include_icloud_api(self):
        self.assertGreaterEqual(self.html.count('value="icloud_api"'), 2)
        self.assertIn("iCloud API: email----Token", self.html)

    def test_pool_label_and_masked_token_field_exist(self):
        self.assertIn("icloud_api:'iCloud API'", self.html)
        self.assertIn("r.token_masked", self.html)

    def test_icloud_import_never_uses_registered_account_mode(self):
        self.assertIn("source === 'icloud_api' ? false", self.html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run template tests and observe missing UI contracts**

```powershell
python -m unittest tests.test_webui_icloud_template -v
```

Expected: all three tests fail.

- [ ] **Step 3: Add iCloud options and import guidance**

Update the import description and textarea placeholder:

```html
<p>Outlook：邮箱----密码----clientId----refreshToken；通用API：邮箱----取码地址；iCloud API：邮箱----Token（---- 或 ==== 分隔均可）。</p>
```

```html
<option value="icloud_api">iCloud API 邮箱池</option>
```

Add the option to both `#importSource` and `#poolSource`, and include:

```text
iCloud API: email----Token
```

in the import textarea placeholder.

- [ ] **Step 4: Render masked tokens and prevent raw credential copy**

Extend `poolLabel`:

```javascript
function poolLabel(src) {
  return ({outlook:'Outlook', generic_api:'通用API', icloud_api:'iCloud API', cloudflare_domain:'域名邮箱'})[src] || src || '-';
}
```

Change the row renderer from an expression callback to a block callback and compute a source-aware preview:

```javascript
$('#outlookBody').innerHTML = rows.map(r => {
  const tokenPreview = r.source === 'icloud_api'
    ? (r.token_masked || '已配置')
    : (short(r.access_token || '', 36) || '未生成');
  const tokenCopyButton = r.source === 'icloud_api'
    ? ''
    : cbtn('复制Token', r.access_token, 'primary');
  return `
    <tr>
      <td><input type="checkbox" class="outlook-row-check" data-email="${esc(r.email)}" data-source="${esc(r.source || 'outlook')}" ${OUTLOOK_SELECTED.has(poolKey(r)) ? 'checked' : ''}></td>
      <td><div class="main-cell">${esc(r.email)}</div><div class="sub-cell mono">${esc(short(r.copy_line, 70))}</div></td>
      <td>${esc(poolLabel(r.source))}</td>
      <td>${pill(r.status)}</td>
      <td><span class="mono">${esc(tokenPreview)}</span></td>
      <td class="muted">${esc(r.imported_at || r.created_at || '-')}</td>
      <td class="muted">${esc(r.used_at || '-')}</td>
      <td class="actions">
        ${cbtn('复制邮箱', r.email)} ${tokenCopyButton}
      </td>
    </tr>`;
}).join('') || '<tr><td colspan="8" class="muted">邮箱池为空</td></tr>';
```

Merge the existing status and delete action buttons into the `<td class="actions">` block shown above. Keep “复制邮箱” available for every source, and never add a raw iCloud Token copy button.

- [ ] **Step 5: Make import results show updates and invalid rows**

In the import click handler, force pool mode for iCloud:

```javascript
const as_registered = source === 'icloud_api' ? false : ($('#importAsRegistered')?.checked ?? true);
```

Render the four counters when the source is iCloud:

```javascript
const summary = source === 'icloud_api'
  ? `解析 ${r.parsed} 行，新增 ${r.inserted || 0}，更新 ${r.updated || 0}，重复 ${r.skipped || 0}，无效 ${r.invalid || 0}`
  : `解析 ${r.parsed} 行，新增 ${r.inserted}，跳过 ${r.skipped}`;
$('#importResult').innerHTML = `<div class="banner info">${esc(summary)}</div>`;
```

Disable and uncheck `#importAsRegistered` while `#importSource` is `icloud_api`; restore it for Outlook and Generic API.

- [ ] **Step 6: Run frontend tests**

```powershell
python -m unittest tests.test_webui_icloud_template -v
```

Expected: all template contract tests pass.

- [ ] **Step 7: Commit frontend integration**

```powershell
git add tests/test_webui_icloud_template.py webui/templates/index.html
git commit -m "feat: add icloud mailbox import ui"
```

---

### Task 7: Documentation and source guidance

**Files:**
- Modify: `README.md`
- Modify: `config/email.py`

- [ ] **Step 1: Add README setup and batch import instructions**

Add an `iCloud Pickup 邮箱` subsection documenting:

```dotenv
EMAIL_SOURCE=icloud_api
ICLOUD_PICKUP_API_BASE=https://icloud.flysms.top/icloud/api/pickup
ICLOUD_PICKUP_TIMEOUT=15
```

Document the WebUI import format:

```text
mail01@icloud.com----tok_xxxxxxxxx
mail02@icloud.com----tok_yyyyyyyyy
```

State explicitly that each Token belongs to one mailbox, raw Tokens are not shown in the pool table or task logs, `401/403` disables the row, and `429` follows `Retry-After`.

- [ ] **Step 2: Update all source lists**

Ensure `README.md`, `config/email.py`, `.env.example`, and `webui/config_editor.py` list the same source set:

```text
outlook,generic_api,icloud_api,cloudflare_domain,cloudflare,gptmail,mailnest,cloudmail
```

- [ ] **Step 3: Verify documentation consistency**

Run:

```powershell
Select-String -Path README.md,config/email.py,.env.example,webui/config_editor.py -Pattern 'icloud_api|ICLOUD_PICKUP'
```

Expected: all four files contain the provider name and the relevant configuration guidance.

- [ ] **Step 4: Commit documentation**

```powershell
git add README.md config/email.py .env.example webui/config_editor.py
git commit -m "docs: document icloud mailbox provider"
```

---

### Task 8: Regression verification and secret-leak audit

**Files:**
- Modify only files needed to correct failures found by the commands below.

- [ ] **Step 1: Run all iCloud-focused tests**

```powershell
python -m unittest \
  tests.test_icloud_config \
  tests.test_icloud_pool \
  tests.test_icloud_mail_client \
  tests.test_icloud_email_provider \
  tests.test_webui_icloud \
  tests.test_webui_icloud_template -v
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete test suite**

```powershell
python -m unittest discover -s tests -v
```

Expected: existing Outlook, Cloudflare, GPTMail, MailNest, CloudMail, configuration, and WebUI tests continue to pass.

- [ ] **Step 3: Run syntax compilation**

```powershell
python -m compileall -q core config webui tests
```

Expected: exit code `0` with no syntax errors.

- [ ] **Step 4: Audit tracked changes for raw credential output**

```powershell
git diff --check
git diff | Select-String -Pattern 'token_masked|Authorization|X-Mailbox-Email|tok_' -Context 2,2
```

Expected: request construction contains the Token only inside the `Authorization` header; list/API/template paths use `token_masked`; no logging statement formats `account.token` or a raw imported Token.

- [ ] **Step 5: Confirm the user's pre-existing Roxy configuration change remains separate**

```powershell
git status --short
git diff -- config/roxybrowser.py
```

Expected: `config/roxybrowser.py` still contains the user's workspace/project ID change and was not included in feature commits.

- [ ] **Step 6: Commit any final test-only corrections**

If verification required tracked corrections, stage only those files and commit:

```powershell
git add core config webui tests README.md .env.example .gitignore
git commit -m "test: verify icloud mailbox isolation"
```

If `git status --short` shows only the pre-existing `config/roxybrowser.py` modification, do not create an empty commit.
