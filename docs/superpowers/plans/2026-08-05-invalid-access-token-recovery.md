# Invalid Access Token Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有“仅给空 AT 补值”修正为“对已有但失效的 AT 重新网页登录并安全替换”，同时支持 HTTP 401 自动资格判断和手动强制重补。

**Architecture:** 数据库层负责 HTTP 401 资格判断、原子领取和基于旧 Token 的 compare-and-swap 替换；后台服务始终执行真实网页登录，不再对已有 Token 短路；API 显式传递 `force`；WebUI 对失效账号显示“AT失效/重补AT”，并允许选中任意账号强制重补。失败、停止、相同 Token 和并发变更均保留旧 Token。

**Tech Stack:** Python 3、Flask、RoxyBrowser/Selenium、本地 JSON 原子文件写入、原生 JavaScript、pytest/unittest。

---

## 文件结构

### 修改

- `core/db.py`：失效判定、领取资格、旧 Token 比较和原子替换。
- `core/access_token_recovery_service.py`：传递 `force`、移除已有 Token 短路、保存旧 Token 并校验新 Token。
- `webui/app.py`：账号列表暴露 `access_token_invalid`，单个和批量接口接收 `force`。
- `webui/templates/index.html`：失效标记、单账号重补、任意账号强制重补和批量重补。
- `README.md`：把“补缺失 AT”说明修正为“失效 AT 重补”。
- `docs/superpowers/specs/2026-08-05-missing-access-token-recovery-design.md`：标记旧设计已被失效 AT 设计替代。
- `tests/test_access_token_recovery_db.py`：资格和原子替换测试。
- `tests/test_access_token_recovery_service.py`：已有 Token 真实登录、相同 Token 和失败保留测试。
- `tests/test_webui_access_token_recovery.py`：`force` 参数和失效字段测试。
- `tests/test_webui_access_token_recovery_template.py`：WebUI 文案、失效标记和请求体契约测试。

### 不新增生产模块

现有模块边界已经足够，本次只修正业务规则，不引入新的队列或浏览器实现。

---

### Task 1: 数据库支持失效资格与原子替换

**Files:**
- Modify: `core/db.py:980-1165,1560-1645`
- Modify: `tests/test_access_token_recovery_db.py`

- [ ] **Step 1: 写失效资格失败测试**

把 `tests/test_access_token_recovery_db.py` 的账号 fixture 扩展为：

```python
self.accounts = [
    {"id": 1, "email": "missing@example.com", "access_token": ""},
    {
        "id": 2,
        "email": "invalid@example.com",
        "access_token": "TOKEN_OLD_INVALID",
        "plan_check_ok": False,
        "plan_check_error": "HTTP 401",
    },
    {
        "id": 3,
        "email": "normal@example.com",
        "access_token": "TOKEN_OLD_NORMAL",
        "plan_check_ok": True,
        "plan_check_error": None,
    },
]
```

新增测试：

```python
def test_claim_accepts_http_401_account_without_force(self):
    result = db.claim_account_access_token_recovery(
        2,
        trigger="auto_invalid",
        log_file="C:/logs/invalid.log",
        force=False,
    )

    self.assertTrue(result["accepted"])
    self.assertEqual(self.accounts[1]["at_recovery_status"], "queued")
    self.assertFalse(self.accounts[1]["at_recovery_force"])

def test_claim_requires_force_for_existing_non_401_token(self):
    skipped = db.claim_account_access_token_recovery(
        3,
        trigger="manual",
        log_file="C:/logs/normal.log",
        force=False,
    )
    forced = db.claim_account_access_token_recovery(
        3,
        trigger="manual",
        log_file="C:/logs/normal-force.log",
        force=True,
    )

    self.assertTrue(skipped["skipped"])
    self.assertIn("HTTP 401", skipped["error"])
    self.assertTrue(forced["accepted"])
    self.assertTrue(self.accounts[2]["at_recovery_force"])

def test_missing_token_remains_eligible_without_force(self):
    result = db.claim_account_access_token_recovery(
        1,
        trigger="manual",
        log_file="C:/logs/missing.log",
        force=False,
    )
    self.assertTrue(result["accepted"])
```

- [ ] **Step 2: 运行资格测试并确认按旧逻辑失败**

Run:

```powershell
python -m pytest tests/test_access_token_recovery_db.py::AccessTokenRecoveryDbTests::test_claim_accepts_http_401_account_without_force tests/test_access_token_recovery_db.py::AccessTokenRecoveryDbTests::test_claim_requires_force_for_existing_non_401_token tests/test_access_token_recovery_db.py::AccessTokenRecoveryDbTests::test_missing_token_remains_eligible_without_force -q
```

Expected: FAIL，原因是 `claim_account_access_token_recovery()` 尚未接受 `force`，且已有 Token 被直接跳过。

- [ ] **Step 3: 实现统一失效判定和领取规则**

在 `core/db.py` 的补 AT 状态机前加入：

```python
def is_account_access_token_invalid(row: dict | None) -> bool:
    row = row or {}
    if not str(row.get("access_token") or "").strip():
        return False
    error = str(row.get("plan_check_error") or "").strip().lower()
    return bool(row.get("plan_check_ok") is False and "401" in error)
```

把领取函数改为：

```python
def claim_account_access_token_recovery(
    acc_id: int,
    *,
    trigger: str,
    log_file: str,
    force: bool = False,
) -> dict:
    """原子领取缺失、HTTP 401 失效或手动强制重补的账号。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return {"accepted": False, "busy": False, "skipped": True, "error": "账号不存在"}
        if str(row.get("at_recovery_status") or "") in {"queued", "running"}:
            return {
                "accepted": False,
                "busy": True,
                "skipped": False,
                "error": "该账号正在重补 AT",
            }

        has_token = bool(str(row.get("access_token") or "").strip())
        invalid = is_account_access_token_invalid(row)
        if has_token and not invalid and not bool(force):
            return {
                "accepted": False,
                "busy": False,
                "skipped": True,
                "error": "账号 AT 未标记为 HTTP 401；如需重补请使用强制模式",
            }

        now = _now()
        row.update({
            "at_recovery_status": "queued",
            "at_recovery_error": None,
            "at_recovery_trigger": str(trigger or "manual"),
            "at_recovery_force": bool(force),
            "at_recovery_queued_at": now,
            "at_recovery_started_at": None,
            "at_recovery_completed_at": None,
            "at_recovery_log_file": str(log_file or ""),
            "at_recovery_stop_requested": False,
            "updated_at": now,
        })
        _save_accounts(accounts)
        return {
            "accepted": True,
            "busy": False,
            "skipped": False,
            "account_id": int(row.get("id") or 0),
            "email": str(row.get("email") or ""),
            "access_token_invalid": invalid,
            "forced": bool(force),
        }
```

- [ ] **Step 4: 写原子替换和旧 Token 保留失败测试**

在数据库测试中新增：

```python
def test_success_replaces_invalid_token_only_when_new_token_differs(self):
    db.claim_account_access_token_recovery(
        2, trigger="auto_invalid", log_file="C:/logs/invalid.log", force=False
    )
    db.mark_account_access_token_recovery_running(2)
    result = db.complete_account_access_token_recovery(
        2,
        session_info={"accessToken": "TOKEN_NEW", "user": {}, "account": {}},
        device_id="device-2",
        proxy_used="http://proxy-2",
        previous_access_token="TOKEN_OLD_INVALID",
    )

    self.assertTrue(result["updated"])
    self.assertTrue(result["replaced"])
    self.assertEqual(self.accounts[1]["access_token"], "TOKEN_NEW")

def test_same_or_concurrently_changed_token_is_not_overwritten(self):
    self.accounts[1]["at_recovery_status"] = "running"
    with self.assertRaisesRegex(ValueError, "新的 Access Token"):
        db.complete_account_access_token_recovery(
            2,
            session_info={"accessToken": "TOKEN_OLD_INVALID"},
            device_id="device-2",
            proxy_used="http://proxy-2",
            previous_access_token="TOKEN_OLD_INVALID",
        )
    self.assertEqual(self.accounts[1]["access_token"], "TOKEN_OLD_INVALID")

    self.accounts[1]["access_token"] = "TOKEN_CHANGED_ELSEWHERE"
    with self.assertRaisesRegex(RuntimeError, "已被其他任务更新"):
        db.complete_account_access_token_recovery(
            2,
            session_info={"accessToken": "TOKEN_NEW"},
            device_id="device-2",
            proxy_used="http://proxy-2",
            previous_access_token="TOKEN_OLD_INVALID",
        )
    self.assertEqual(self.accounts[1]["access_token"], "TOKEN_CHANGED_ELSEWHERE")
```

- [ ] **Step 5: 运行替换测试并确认旧实现失败**

```powershell
python -m pytest tests/test_access_token_recovery_db.py::AccessTokenRecoveryDbTests::test_success_replaces_invalid_token_only_when_new_token_differs tests/test_access_token_recovery_db.py::AccessTokenRecoveryDbTests::test_same_or_concurrently_changed_token_is_not_overwritten -q
```

Expected: FAIL，原因是完成函数仍把已有 Token 当作 `already_present`，也没有旧 Token 比较参数。

- [ ] **Step 6: 实现 compare-and-swap 替换**

把 `complete_account_access_token_recovery()` 增加参数并替换已有 Token 短路：

```python
def complete_account_access_token_recovery(
    acc_id: int,
    *,
    session_info: dict,
    device_id: str,
    proxy_used: str | None,
    previous_access_token: str = "",
) -> dict:
    token = str((session_info or {}).get("accessToken") or "").strip()
    if not token:
        raise ValueError("session_info 缺少 accessToken")
    previous = str(previous_access_token or "").strip()
    if previous and token == previous:
        raise ValueError("登录未返回新的 Access Token")
    user = (session_info or {}).get("user") or {}
    account = (session_info or {}).get("account") or {}

    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return {"updated": False, "replaced": False, "error": "账号不存在"}
        current = str(row.get("access_token") or "").strip()
        if current != previous:
            raise RuntimeError("账号 Access Token 已被其他任务更新，本次结果未写入")

        extra = {}
        try:
            parsed = json.loads(str(row.get("extra_json") or "{}"))
            if isinstance(parsed, dict):
                extra = parsed
        except Exception:
            pass
        extra.update({
            "user": user,
            "account": account,
            "expires": (session_info or {}).get("expires"),
        })

        row["access_token"] = token
        if user.get("id") is not None:
            row["user_id"] = user.get("id")
        if user.get("name") is not None:
            row["user_name"] = user.get("name")
        if account.get("planType") is not None:
            row["plan_type"] = account.get("planType")
        if (session_info or {}).get("expires") is not None:
            row["expires_at"] = (session_info or {}).get("expires")
        row["device_id"] = str(device_id or "").strip()
        if str(proxy_used or "").strip():
            row["proxy_used"] = str(proxy_used).strip()
        row["extra_json"] = json.dumps(extra, ensure_ascii=False)
        row["at_recovery_status"] = "success"
        row["at_recovery_error"] = None
        row["at_recovery_stop_requested"] = False
        row["at_recovery_completed_at"] = _now()
        row["updated_at"] = row["at_recovery_completed_at"]
        _save_accounts(accounts)
        return {"updated": True, "replaced": bool(previous)}
```

在账号分页和轻量状态快照中增加：

```python
item["access_token_invalid"] = is_account_access_token_invalid(row)
```

- [ ] **Step 7: 运行数据库测试**

```powershell
python -m pytest tests/test_access_token_recovery_db.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交数据库修正**

```powershell
git add core/db.py tests/test_access_token_recovery_db.py
git commit -m "fix: allow invalid access token replacement"
```

---

### Task 2: 后台服务对已有 Token 执行真实登录

**Files:**
- Modify: `core/access_token_recovery_service.py:98-205`
- Modify: `tests/test_access_token_recovery_service.py`

- [ ] **Step 1: 写已有 Token 登录和参数传递失败测试**

新增：

```python
def test_worker_relogs_existing_invalid_token_and_passes_previous_token(self):
    account = {
        "id": 7,
        "email": "invalid@example.com",
        "access_token": "TOKEN_OLD_INVALID",
        "proxy_used": "http://saved-proxy",
        "device_id": "device-7",
        "plan_check_ok": False,
        "plan_check_error": "HTTP 401",
    }
    with patch.object(service.db, "get_account", return_value=account), \
         patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
         patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
         patch.object(service, "run_roxy_access_token_recovery", return_value={
             "session_info": {"accessToken": "TOKEN_NEW"},
             "device_id": "device-7",
             "proxy_used": "http://saved-proxy",
         }) as run, \
         patch.object(service.db, "complete_account_access_token_recovery", return_value={
             "updated": True,
             "replaced": True,
         }) as complete:
        result = service._run_recovery(account_id=7, trigger="auto_invalid", force=False)

    self.assertTrue(result["ok"])
    run.assert_called_once()
    self.assertEqual(
        complete.call_args.kwargs["previous_access_token"],
        "TOKEN_OLD_INVALID",
    )

def test_enqueue_passes_force_to_claim_and_worker(self):
    fake_future = object()
    with patch.object(service._QUEUE_SLOTS, "acquire", return_value=True), \
         patch.object(service.db, "claim_account_access_token_recovery", return_value={
             "accepted": True, "busy": False, "skipped": False,
         }) as claim, \
         patch.object(service, "_event_for"), \
         patch.object(service._EXECUTOR, "submit", return_value=fake_future) as submit:
        result = service.enqueue_account_access_token_recovery(
            account_id=9,
            trigger="manual",
            force=True,
        )

    self.assertIs(result["future"], fake_future)
    self.assertTrue(claim.call_args.kwargs["force"])
    self.assertTrue(submit.call_args.kwargs["force"])
```

- [ ] **Step 2: 运行并确认旧短路逻辑导致失败**

```powershell
python -m pytest tests/test_access_token_recovery_service.py::AccessTokenRecoveryServiceTests::test_worker_relogs_existing_invalid_token_and_passes_previous_token tests/test_access_token_recovery_service.py::AccessTokenRecoveryServiceTests::test_enqueue_passes_force_to_claim_and_worker -q
```

Expected: FAIL，原因是 `_run_recovery()` 和入队函数没有 `force` 参数，已有 Token 会直接成功短路。

- [ ] **Step 3: 移除已有 Token 短路并贯穿 `force`**

修改签名和主流程：

```python
def _run_recovery(*, account_id: int, trigger: str, force: bool = False) -> dict:
    try:
        if not db.mark_account_access_token_recovery_running(account_id):
            account = db.get_account(account_id) or {}
            if account.get("at_recovery_status") == "stopped":
                return {"ok": False, "status": "stopped", "error": "用户手动停止"}
            return {
                "ok": False,
                "status": "failed",
                "error": "账号已删除或重补 AT 状态已重置",
            }

        account = db.get_account(account_id)
        if not account:
            raise RuntimeError("账号不存在")
        previous_access_token = str(account.get("access_token") or "").strip()
        proxy = (
            str(account.get("proxy_used") or "").strip()
            or str(proxy_cfg.pick_proxy() or "").strip()
            or None
        )
        device_id = str(account.get("device_id") or "").strip() or str(uuid.uuid4())
        result = run_roxy_access_token_recovery(
            email=str(account.get("email") or ""),
            proxy=proxy,
            device_id=device_id,
            should_stop=lambda: _should_stop(account_id),
        )
        persisted = db.complete_account_access_token_recovery(
            account_id,
            session_info=result["session_info"],
            device_id=result["device_id"],
            proxy_used=result.get("proxy_used"),
            previous_access_token=previous_access_token,
        )
        return {"ok": True, "status": "success", **persisted}
    except AccessTokenRecoveryStopped as exc:
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(account_id, error=error, status="stopped")
        return {"ok": False, "status": "stopped", "error": error}
    except Exception as exc:
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(account_id, error=error, status="failed")
        logger.error("[重补AT] 账号恢复失败: account_id=%s error=%s", account_id, error)
        return {"ok": False, "status": "failed", "error": error}
```

修改日志包装和入队：

```python
def _run_recovery_with_log(
    *, account_id: int, trigger: str, force: bool, log_file: str
) -> dict:
    try:
        with _RecoveryLogContext(log_file):
            logger.info(
                "[重补AT] 开始: account_id=%s trigger=%s force=%s",
                account_id,
                trigger,
                bool(force),
            )
            result = _run_recovery(
                account_id=account_id,
                trigger=trigger,
                force=force,
            )
            logger.info(
                "[重补AT] 完成: account_id=%s status=%s",
                account_id,
                result.get("status"),
            )
            return result
    finally:
        with _EVENTS_LOCK:
            _STOP_EVENTS.pop(int(account_id), None)
        _QUEUE_SLOTS.release()

def enqueue_account_access_token_recovery(
    *, account_id: int, trigger: str = "manual", force: bool = False
) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {
            "accepted": False,
            "busy": False,
            "skipped": False,
            "error": "重补 AT 队列已满",
        }
    log_file = str(_LOG_DIR / f"at-recovery-{int(account_id)}-{uuid.uuid4().hex}.log")
    claim = db.claim_account_access_token_recovery(
        int(account_id),
        trigger=trigger,
        log_file=log_file,
        force=bool(force),
    )
    if not claim.get("accepted"):
        _QUEUE_SLOTS.release()
        return claim
    _event_for(int(account_id))
    try:
        future = _EXECUTOR.submit(
            _run_recovery_with_log,
            account_id=int(account_id),
            trigger=trigger,
            force=bool(force),
            log_file=log_file,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        with _EVENTS_LOCK:
            _STOP_EVENTS.pop(int(account_id), None)
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(int(account_id), error=error)
        return {"accepted": False, "busy": False, "skipped": False, "error": error}
    return {**claim, "future": future}
```

- [ ] **Step 4: 增加相同 Token 失败保留测试**

```python
def test_same_token_failure_is_persisted_without_exposing_token(self):
    account = {
        "id": 8,
        "email": "invalid@example.com",
        "access_token": "TOKEN_OLD_INVALID",
        "proxy_used": "http://saved-proxy",
        "device_id": "device-8",
    }
    with patch.object(service.db, "get_account", return_value=account), \
         patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
         patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
         patch.object(service, "run_roxy_access_token_recovery", return_value={
             "session_info": {"accessToken": "TOKEN_OLD_INVALID"},
             "device_id": "device-8",
             "proxy_used": "http://saved-proxy",
         }), \
         patch.object(
             service.db,
             "complete_account_access_token_recovery",
             side_effect=ValueError("登录未返回新的 Access Token"),
         ), \
         patch.object(service.db, "fail_account_access_token_recovery") as fail:
        result = service._run_recovery(account_id=8, trigger="manual", force=True)

    self.assertFalse(result["ok"])
    self.assertEqual(result["error"], "登录未返回新的 Access Token")
    self.assertNotIn("TOKEN_OLD_INVALID", str(result))
    fail.assert_called_once()
```

- [ ] **Step 5: 运行服务测试**

```powershell
python -m pytest tests/test_access_token_recovery_service.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交服务修正**

```powershell
git add core/access_token_recovery_service.py tests/test_access_token_recovery_service.py
git commit -m "fix: relog accounts with invalid access tokens"
```

---

### Task 3: API 暴露失效状态并传递强制模式

**Files:**
- Modify: `webui/app.py:140-205,417-480`
- Modify: `tests/test_webui_access_token_recovery.py`

- [ ] **Step 1: 写 API `force` 契约失败测试**

修改单个测试并新增批量断言：

```python
def test_single_recovery_passes_force(self):
    with patch("webui.app.access_token_recovery_service.enqueue_account_access_token_recovery", return_value={
        "accepted": True,
        "busy": False,
        "skipped": False,
        "account_id": 168,
        "email": "invalid@example.com",
    }) as enqueue:
        response = self.client.post(
            "/api/accounts/recover-access-token",
            json={"account_id": 168, "force": True},
        )

    self.assertEqual(response.status_code, 202)
    enqueue.assert_called_once_with(account_id=168, trigger="manual", force=True)

def test_bulk_recovery_passes_force_to_every_account(self):
    with patch(
        "webui.app.access_token_recovery_service.enqueue_account_access_token_recovery",
        side_effect=[
            {"accepted": True, "busy": False, "skipped": False, "account_id": 168},
            {"accepted": True, "busy": False, "skipped": False, "account_id": 167},
        ],
    ) as enqueue:
        response = self.client.post(
            "/api/accounts/recover-access-token-bulk",
            json={"account_ids": [168, 167], "force": True},
        )

    self.assertEqual(response.status_code, 202)
    self.assertEqual(enqueue.call_count, 2)
    self.assertTrue(all(call.kwargs["force"] for call in enqueue.call_args_list))
```

在紧凑账号测试加入：

```python
account.update({
    "access_token": "TOKEN_INVALID",
    "plan_check_ok": False,
    "plan_check_error": "HTTP 401",
})
with patch("webui.app.db.is_account_access_token_invalid", return_value=True):
    response = self.client.get("/api/accounts?paged=1")
self.assertTrue(response.get_json()["items"][0]["access_token_invalid"])
```

- [ ] **Step 2: 运行并确认失败**

```powershell
python -m pytest tests/test_webui_access_token_recovery.py -q
```

Expected: FAIL，接口尚未传递 `force`，账号列表尚未暴露失效布尔值。

- [ ] **Step 3: 实现 API 和列表字段**

在 `_compact_account()` 的输出中加入：

```python
"access_token_invalid": db.is_account_access_token_invalid(row),
```

单个接口加入：

```python
force = data.get("force") is True
result = access_token_recovery_service.enqueue_account_access_token_recovery(
    account_id=account_id,
    trigger="manual",
    force=force,
)
```

批量接口在循环前加入：

```python
force = data.get("force") is True
```

并把每次入队改为：

```python
result = access_token_recovery_service.enqueue_account_access_token_recovery(
    account_id=account_id,
    trigger="manual_bulk",
    force=force,
)
```

- [ ] **Step 4: 运行 API 测试**

```powershell
python -m pytest tests/test_webui_access_token_recovery.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交 API 修正**

```powershell
git add webui/app.py tests/test_webui_access_token_recovery.py
git commit -m "fix: expose invalid token recovery mode"
```

---

### Task 4: WebUI 改为失效 AT 重补

**Files:**
- Modify: `webui/templates/index.html:560-570,1656-1676,1758-1807`
- Modify: `tests/test_webui_access_token_recovery_template.py`

- [ ] **Step 1: 写 WebUI 契约失败测试**

新增：

```python
def test_toolbar_and_rows_describe_invalid_token_recovery(self):
    self.assertIn(">重补AT</button>", self.html)
    self.assertIn("AT失效", self.html)
    self.assertIn("重补AT", self.html)
    self.assertNotIn(">补缺失AT</button>", self.html)

def test_single_and_bulk_requests_send_force_mode(self):
    self.assertIn("force: force", self.html)
    self.assertIn("JSON.stringify({account_ids: ids, force: true})", self.html)

def test_http_401_accounts_use_automatic_eligibility(self):
    self.assertIn("const force = account.has_access_token && !account.access_token_invalid", self.html)
```

- [ ] **Step 2: 运行并确认旧文案和旧筛选导致失败**

```powershell
python -m pytest tests/test_webui_access_token_recovery_template.py -q
```

Expected: FAIL。

- [ ] **Step 3: 修改按钮、Token 状态和行操作**

工具栏改为：

```html
<button class="btn primary" id="btnRecoverSelectedAccessTokens" disabled title="给选中的账号重新登录并替换失效 Access Token">重补AT</button>
```

替换两个渲染函数：

```javascript
function _accessTokenCell(r) {
  const status = r.at_recovery_status || '';
  const error = r.at_recovery_error || '';
  if (status === 'queued') return '<span class="pill status-running">重补AT排队中</span>';
  if (status === 'running') return '<span class="pill status-running">重补AT中</span>';
  if (status === 'failed') return `<div class="extract-link-cell"><span class="pill status-failed" title="${esc(error)}">重补AT失败</span><div class="extract-link-error">${esc(error)}</div></div>`;
  if (status === 'stopped') return `<span class="pill status-used" title="${esc(error)}">重补AT已停止</span>`;
  if (r.access_token_invalid) return '<span class="pill status-failed" title="套餐检测返回 HTTP 401">AT失效</span>';
  if (r.has_access_token) return '<span class="mono" title="列表仅显示状态，完整 Token 请点复制">有Token</span>';
  return '<span class="muted">无Token</span>';
}

function _accessTokenRecoveryAction(r) {
  const status = r.at_recovery_status || '';
  const log = r.has_at_recovery_log
    ? `<button data-at-recovery-log="${esc(r.id)}" title="查看最近一次重补 AT 日志">重补日志</button>`
    : '';
  if (['queued', 'running'].includes(status)) {
    return `<button class="danger" data-at-recovery-stop="${esc(r.id)}" title="停止该账号重补 AT">停止重补</button>${log}`;
  }
  const label = r.has_access_token ? '重补AT' : '补AT';
  const cls = r.access_token_invalid ? 'danger' : 'primary';
  return `<button class="${cls}" data-at-recover="${esc(r.id)}" title="通过网页版登录和邮箱验证码获取新的 Access Token">${label}</button>${log}`;
}
```

- [ ] **Step 4: 修改单账号和批量请求**

替换 `recoverOneAccessToken()`：

```javascript
async function recoverOneAccessToken(id, btn) {
  const account = ACCOUNTS.find(item => Number(item.id) === Number(id));
  if (!account) { showToast('账号不存在'); return; }
  const force = account.has_access_token && !account.access_token_invalid;
  const action = account.has_access_token ? '重补并替换 AT' : '补 AT';
  const reason = account.access_token_invalid
    ? '该账号套餐检测返回 HTTP 401，已标记为 AT 失效。'
    : (force ? '该账号未标记 HTTP 401，本次将强制重新登录。' : '该账号当前没有 AT。');
  if (!confirm(`确定为该账号${action}吗？\n\n${account.email || ('#' + id)}\n\n${reason}\n成功后才写入新 AT；失败会保留旧 AT。`)) return;
  btn.disabled = true;
  try {
    const result = await api('/api/accounts/recover-access-token', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({account_id: id, force: force}),
    });
    showToast(result.started ? '重补 AT 已加入后台队列' : (result.error || '账号已跳过'));
    await pollAccountPlanStatuses();
  } catch (error) {
    showToast('重补 AT 提交失败: ' + error.message);
    await pollAccountPlanStatuses();
  } finally {
    btn.disabled = false;
  }
}
```

替换批量函数的资格和请求部分：

```javascript
const eligible = selected.filter(item => !['queued', 'running'].includes(item.at_recovery_status));
const busy = selected.length - eligible.length;
if (!eligible.length) { showToast('选中账号都在重补 AT'); return; }
const invalid = eligible.filter(item => item.access_token_invalid).length;
const forced = eligible.filter(item => item.has_access_token && !item.access_token_invalid).length;
const missing = eligible.filter(item => !item.has_access_token).length;
if (!confirm(`确定批量重补 ${eligible.length} 个账号吗？\n\nHTTP 401 失效 ${invalid} 个；普通强制重补 ${forced} 个；缺失 AT ${missing} 个；运行中跳过 ${busy} 个。\n\n成功后才替换，失败保留旧 AT。`)) return;
const result = await api('/api/accounts/recover-access-token-bulk', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({account_ids: ids, force: true}),
});
```

- [ ] **Step 5: 运行模板和 Node 语法检查**

```powershell
python -m pytest tests/test_webui_access_token_recovery_template.py -q
@'
const fs = require('fs');
const html = fs.readFileSync('webui/templates/index.html', 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
for (const [i, source] of scripts.entries()) {
  new Function(source);
  console.log(`script ${i + 1}: ok`);
}
'@ | node
```

Expected: pytest PASS，所有脚本输出 `ok`。

- [ ] **Step 6: 提交 WebUI 修正**

```powershell
git add webui/templates/index.html tests/test_webui_access_token_recovery_template.py
git commit -m "fix: add invalid access token recovery controls"
```

---

### Task 5: 文档、全量回归和真实 HTTP 401 验收

**Files:**
- Modify: `README.md:524-535`
- Modify: `docs/superpowers/specs/2026-08-05-missing-access-token-recovery-design.md:1-5`

- [ ] **Step 1: 修正文档**

把 README 章节替换为：

```markdown
### 给失效账号重补 Access Token

账号页支持对已有但失效的 Access Token 重新登录获取新 AT。套餐查询返回 HTTP 401 的账号会标记“AT失效”；也可以手动选择任意账号强制重补。

- 支持单账号和勾选批量重补。
- 成功取得不同的新 AT 后才替换旧 AT。
- 失败、停止、手机号验证、账号停用或返回相同 AT 时保留旧 AT。
- 优先复用账号保存的代理和 `device_id`，通过原邮箱来源读取一次性验证码。
- 缺少 AT 的账号仍可沿用同一流程补全。
- 成功后同步账号 JSON、账号 TXT、Token TXT 和静态查看页。
- 排队或运行中的任务可停止；账号行可查看最近一次重补日志。
```

在旧设计文件标题下加入：

```markdown
> 此设计的“仅补缺失 AT”范围已被 `2026-08-05-invalid-access-token-recovery-design.md` 替代；实现以失效 AT 重补设计为准。
```

- [ ] **Step 2: 运行相关测试**

```powershell
python -m pytest tests/test_access_token_recovery_db.py tests/test_access_token_recovery_service.py tests/test_roxy_access_token_recovery.py tests/test_webui_access_token_recovery.py tests/test_webui_access_token_recovery_template.py -q
```

Expected: PASS。

- [ ] **Step 3: 运行全量测试和静态检查**

```powershell
python -m pytest -q
git diff --check
git grep -n -E "eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|Bearer [A-Za-z0-9._-]{40,}" -- core tests webui README.md docs
```

Expected: 全量测试 PASS；`git diff --check` 无输出；敏感 Token grep 无真实命中。

- [ ] **Step 4: 提交文档**

```powershell
git add README.md docs/superpowers/specs/2026-08-05-missing-access-token-recovery-design.md
git commit -m "docs: correct access token recovery behavior"
```

- [ ] **Step 5: 准备最新 HTTP 401 隔离验收账号**

验收使用当前最新数据中的账号 ID `168`，创建时间为 `2026-08-04T23:07:19`，套餐检测错误为 HTTP 401。保持其旧 AT 不变，不再清空 Token。

先记录脱敏指纹和备份：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath '注册成功的邮箱.json' -Destination "注册成功的邮箱.$stamp.invalid-at.backup.json"
Copy-Item -LiteralPath '注册成功的token.txt' -Destination "注册成功的token.$stamp.invalid-at.backup.txt"
@'
import hashlib, json
from pathlib import Path
p = Path("注册成功的邮箱.json")
data = json.loads(p.read_text(encoding="utf-8"))
rows = data if isinstance(data, list) else data.get("accounts", data.get("data", []))
row = next(item for item in rows if int(item.get("id") or 0) == 168)
token = str(row.get("access_token") or "")
print("id=168", "has_token=", bool(token), "fingerprint=", hashlib.sha256(token.encode()).hexdigest()[:12])
print("plan_check_error=", row.get("plan_check_error"))
'@ | python -
```

Expected: `has_token=True`，错误包含 `HTTP 401`，只输出 12 位哈希指纹。

- [ ] **Step 6: WebUI 执行真实重补验收**

1. 重启功能分支 WebUI。
2. 在账号页定位 ID `168`，确认显示“AT失效”和“重补AT”。
3. 点击“重补AT”，确认请求未使用 `force=true`，由 HTTP 401 资格自动通过。
4. 查看重补日志和状态。
5. 若登录成功，重新计算指纹，确认新旧指纹不同且 Token TXT 已同步。
6. 若账号停用、OTP 失败或手机号验证导致任务失败，确认旧指纹完全不变，状态为 `failed` 且错误可读。

Expected: 真实路径直接处理已有 AT；任何失败都不丢失旧 AT。自动化测试负责覆盖成功替换路径，真实账号至少验证 HTTP 401 资格、网页登录启动和失败保留。

- [ ] **Step 7: 最终验证和状态检查**

```powershell
python -m pytest -q
git diff --check
git status --short
```

Expected: 全量测试 PASS；无未提交的源码修改；本地账号备份和验收日志保持忽略状态，不加入 Git。

---

## 完成定义

- HTTP 401 且已有 AT 的账号无需清空 Token 即可进入重补队列。
- 任意已有 AT 的账号可以通过明确确认后强制重补。
- 后台服务对已有 Token 始终执行真实网页登录，不再短路复用旧 Token。
- 新 AT 必须非空且与旧 AT 不同，数据库使用旧 Token compare-and-swap 原子替换。
- 失败、停止、相同 Token 和并发修改都保留原 Token。
- WebUI 清晰显示“AT失效/重补AT”，批量重补覆盖选中的已有 AT 账号。
- API、日志和测试输出不泄露完整 Token。
- 全量测试通过，并用最新 HTTP 401 账号 ID `168` 完成真实验收。
