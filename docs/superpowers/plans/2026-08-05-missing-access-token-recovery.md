# Missing Access Token Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为账号列表中 `access_token` 为空的账号提供单个和批量网页版登录补全能力，自动读取邮箱 OTP，完整写回账号元数据，并支持状态、日志与停止。

**Architecture:** 新增独立的补 AT 浏览器流程和后台服务，不调用注册入口、Codex OAuth 或接码模块。数据库文件层负责原子领取和成功写回；Roxy 公共启动能力增加显式代理与协作式中止参数；WebUI 通过现有账号列表和轻量状态轮询展示任务进度。

**Tech Stack:** Python 3、Flask、Selenium/RoxyBrowser、本地 JSON 文件持久化、`ThreadPoolExecutor`、原生 HTML/CSS/JavaScript、pytest/unittest。

---

## 审计结论

设计可落地，但实现前需要锁定以下四项细节；这些内容已经补入设计文档：

1. `RoxyBrowserClient` 当前只能从代理池创建环境，补 AT 必须新增显式 `proxy_url`，才能真正复用账号 `proxy_used`。
2. 保存的代理可能是 `sid-*:bridge@127.0.0.1:25001`；创建环境前必须调用 `prepare_proxy_for_roxy()` 恢复桥接服务。
3. 设计要求批量任务可停止，因此增加批量停止 API 和按钮，而不是只提供单账号停止。
4. 账号行需要查看独立补 AT 日志，因此增加日志读取 API；只暴露日志内容，不暴露服务器文件路径。

## 文件结构

### 新建

- `core/roxy_access_token_recovery.py`：单账号网页版登录、OTP、session 获取和手机号页拦截。
- `core/access_token_recovery_service.py`：队列、日志、停止、错误清洗和数据库写回编排。
- `tests/test_access_token_recovery_db.py`：数据库状态机和原子写回测试。
- `tests/test_roxy_access_token_recovery.py`：网页版登录流程测试。
- `tests/test_access_token_recovery_service.py`：后台队列、兜底身份、停止和脱敏测试。
- `tests/test_webui_access_token_recovery.py`：Flask API 和列表字段测试。
- `tests/test_webui_access_token_recovery_template.py`：账号页按钮、轮询和日志交互静态契约测试。

### 修改

- `core/roxybrowser_client.py`：新建 Roxy 环境时接受显式代理。
- `core/roxy_registration.py`：共享登录步骤增加允许登录密码页和协作式中止参数。
- `core/db.py`：补 AT 状态字段、原子领取、完成、失败、停止和重启恢复。
- `webui/app.py`：启动恢复、单个/批量/停止/日志 API、账号轻量字段。
- `webui/templates/index.html`：账号行、批量按钮、状态轮询和日志弹窗。
- `README.md`：补 AT 功能、行为边界和操作说明。
- `tests/test_roxy_registration_proxy.py`：显式代理回归测试。
- `tests/test_roxy_startup_gate.py`：启动代理和中止回调回归测试。
- `tests/test_roxy_email_submit.py`：允许恢复流程进入登录密码页的回归测试。
- `tests/test_roxy_otp_transition.py`：OTP/session 等待中止回调测试。

---

### Task 1: 扩展共享 Roxy 登录能力

**Files:**
- Modify: `core/roxybrowser_client.py:457-574`
- Modify: `core/roxy_registration.py:477-624,714-784,1497-1586,1597-1655`
- Modify: `tests/test_roxy_registration_proxy.py`
- Modify: `tests/test_roxy_startup_gate.py`
- Modify: `tests/test_roxy_email_submit.py`
- Modify: `tests/test_roxy_otp_transition.py`

- [ ] **Step 1: 写显式代理与登录密码页的失败测试**

在 `tests/test_roxy_registration_proxy.py` 增加：

```python
def test_explicit_proxy_overrides_pool_and_is_exposed_on_open_result(self):
    client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
    upstream = "http://user-region-US-sid-fixed01:pass@upstream.example:3000"
    bridge = "http://sid-fixed01:bridge@127.0.0.1:25001"
    responses = [
        {"code": 0, "data": {"dirId": "profile-explicit"}},
        {"code": 0, "data": {"http": "127.0.0.1:9222"}},
    ]
    with patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True), \
         patch.object(roxy_cfg, "ROXY_PROFILE_ID", ""), \
         patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "1"), \
         patch.object(roxy_cfg, "ROXY_CREATE_USE_PROXY_POOL", True), \
         patch("config.proxy.pick_proxy") as pick_proxy, \
         patch("core.roxybrowser_client.prepare_proxy_for_roxy", return_value=bridge) as prepare, \
         patch.object(client, "request", side_effect=responses) as request:
        opened = client.open_profile(proxy_url=upstream)

    pick_proxy.assert_not_called()
    prepare.assert_called_once_with(upstream)
    create_body = request.call_args_list[0].kwargs["json_body"]
    self.assertEqual(create_body["proxyInfo"]["host"], "127.0.0.1")
    self.assertEqual(create_body["proxyInfo"]["port"], "25001")
    self.assertEqual(opened.registration_proxy, bridge)
```

在 `tests/test_roxy_email_submit.py` 增加：

```python
def test_recovery_mode_returns_login_password_state(self):
    driver = object()
    with patch.object(roxy_registration, "_type_email_address"), \
         patch.object(roxy_registration, "_email_input_value_state", return_value={
             "inputs": [{"value": "saved@example.com"}],
         }), \
         patch.object(roxy_registration, "human_delay"), \
         patch.object(roxy_registration, "_submit_email_step"), \
         patch.object(roxy_registration, "_wait_email_submit_next_state", return_value="login_password"):
        state = roxy_registration._submit_email_and_wait_next(
            driver,
            "saved@example.com",
            allow_login_password=True,
        )

    self.assertEqual(state, "login_password")
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```powershell
python -m pytest tests/test_roxy_registration_proxy.py::RoxyRegistrationProxyTests::test_explicit_proxy_overrides_pool_and_is_exposed_on_open_result tests/test_roxy_email_submit.py -q
```

Expected: `open_profile()` 不接受 `proxy_url`，或 `_submit_email_and_wait_next()` 不接受 `allow_login_password`。

- [ ] **Step 3: 实现显式代理和可选登录密码页**

在 `core/roxybrowser_client.py` 中把签名和代理选择逻辑改为：

```python
def create_profile(
    self,
    payload: dict | None = None,
    *,
    proxy_url: str | None = None,
) -> tuple[str, str | None]:
    body = dict(getattr(_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}) or {})
    if payload:
        body.update(payload)
    registration_proxy = None

    explicit_proxy = str(proxy_url or "").strip()
    if explicit_proxy:
        prepared_proxy = prepare_proxy_for_roxy(explicit_proxy)
        registration_proxy = prepared_proxy
        body["proxyInfo"] = _proxy_url_to_roxy_info(prepared_proxy)
    elif bool(getattr(_cfg, "ROXY_CREATE_USE_PROXY_POOL", False)) and not body.get("proxyInfo"):
        from config import proxy as _proxy_cfg

        selected_proxy = _proxy_cfg.pick_proxy()
        if selected_proxy:
            prepared_proxy = prepare_proxy_for_roxy(selected_proxy)
            registration_proxy = prepared_proxy
            body["proxyInfo"] = _proxy_url_to_roxy_info(prepared_proxy)
        else:
            logger.warning(
                "[Roxy] 已启用 ROXY_CREATE_USE_PROXY_POOL，但 PROXY_POOL 为空，本次创建环境不设置代理"
            )

    default_os = str(getattr(_cfg, "ROXY_DEFAULT_OS", "macOS") or "macOS").strip()
    if default_os:
        body.setdefault("os", default_os)
    default_os_version = str(getattr(_cfg, "ROXY_DEFAULT_OS_VERSION", "") or "").strip()
    if default_os_version:
        body.setdefault("osVersion", default_os_version)
    workspace_id = _workspace_id_value()
    if workspace_id:
        body.setdefault("workspaceId", workspace_id)
    project_id = _project_id_value()
    if project_id:
        body.setdefault("projectId", project_id)
    if not body.get("workspaceId"):
        raise RuntimeError("Roxy 创建环境需要 workspaceId")

    result = self.request(_cfg.ROXY_CREATE_METHOD, _cfg.ROXY_CREATE_PATH, json_body=body)
    profile_id = _first(result, [
        ("id",), ("dirId",), ("dir_id",), ("profile_id",), ("profileId",),
        ("browser_id",), ("data", "id"), ("data", "dirId"),
        ("data", "dir_id"), ("data", "profile_id"),
        ("data", "profileId"), ("data", "browser_id"),
    ])
    if not profile_id:
        raise RuntimeError(f"Roxy 创建环境成功但未返回 dirId/profile_id: {result}")
    return str(profile_id), registration_proxy


def open_profile(
    self,
    profile_id: str | None = None,
    *,
    proxy_url: str | None = None,
) -> RoxyOpenResult:
    one_profile = bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
    configured_pid = self._normalize_profile_id(
        profile_id if profile_id is not None else getattr(_cfg, "ROXY_PROFILE_ID", "")
    )
    if one_profile and configured_pid:
        raise RuntimeError(
            "已启用 ROXY_ONE_PROFILE_PER_ACCOUNT=True（一号一环境），不能配置固定 ROXY_PROFILE_ID"
        )
    if configured_pid and str(proxy_url or "").strip():
        raise RuntimeError("显式代理只支持新建 Roxy 环境，不能修改已存在环境的代理")

    pid = configured_pid
    created_by_run = False
    registration_proxy = None
    if not pid:
        pid, registration_proxy = self.create_profile(proxy_url=proxy_url)
        created_by_run = True

    path = str(_cfg.ROXY_OPEN_PATH).format(profile_id=pid)
    params = dict(getattr(_cfg, "ROXY_OPEN_EXTRA_PARAMS", {}) or {})
    params.setdefault("workspaceId", _workspace_id_value())
    params.setdefault("dirId", int(pid) if str(pid).isdigit() else pid)
    params.setdefault("args", [])
    params.setdefault("forceOpen", True)
    params["headless"] = bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False))
    result = self.request(
        _cfg.ROXY_OPEN_METHOD,
        path,
        params=params if _cfg.ROXY_OPEN_METHOD.upper() == "GET" else None,
        json_body=params if _cfg.ROXY_OPEN_METHOD.upper() != "GET" else None,
    )
    debugger_address = self._extract_debugger_address(result)
    webdriver_url = _first(result, [
        ("webdriver",), ("webDriver",), ("webdriver_url",), ("webdriverUrl",),
        ("selenium",), ("selenium_url",), ("seleniumUrl",),
        ("data", "webdriver"), ("data", "webDriver"),
        ("data", "webdriver_url"), ("data", "webdriverUrl"),
        ("data", "selenium"), ("data", "selenium_url"),
        ("data", "seleniumUrl"),
    ]) or None
    ws_endpoint = _first(result, [
        ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
        ("data", "ws"), ("data", "wsEndpoint"),
        ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
    ]) or None
    if not debugger_address and not webdriver_url:
        raise RuntimeError(f"Roxy 已打开环境但未返回 Selenium/调试地址: {result}")
    return RoxyOpenResult(
        str(pid),
        result,
        debugger_address=debugger_address,
        webdriver_url=webdriver_url,
        ws_endpoint=ws_endpoint,
        created_by_run=created_by_run,
        registration_proxy=registration_proxy,
    )
```

在 `core/roxy_registration.py` 为邮箱提交增加参数，并保持注册默认行为：

```python
def _run_abort_checker(abort_checker) -> None:
    if abort_checker is not None:
        abort_checker()


def _submit_email_and_wait_next(
    driver,
    email: str,
    attempts: int = 3,
    *,
    allow_login_password: bool = False,
    abort_checker=None,
) -> str:
    last_state = None
    nextauth_fallback_used = False
    for attempt in range(1, attempts + 1):
        _run_abort_checker(abort_checker)
        _type_email_address(driver, email, timeout=20)
        state = _email_input_value_state(driver)
        last_state = state
        values = [str(item.get("value") or "") for item in (state.get("inputs") or [])]
        if not any(value.strip().lower() == email.strip().lower() for value in values):
            time.sleep(0.8)
            continue
        human_delay("form")
        _submit_email_step(driver)
        state_name = _wait_email_submit_next_state(
            driver,
            email,
            timeout=12,
            abort_checker=abort_checker,
        )
        if state_name == "login_password":
            if allow_login_password:
                return state_name
            raise RuntimeError(
                f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: "
                f"url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}"
            )
        if state_name in ("password", "otp", "logged_in"):
            return state_name
        if not nextauth_fallback_used and state_name in ("email_page", "unknown"):
            nextauth_fallback_used = True
            if _navigate_auth_via_nextauth(driver, email):
                state_name = _wait_email_submit_next_state(
                    driver,
                    email,
                    timeout=30,
                    abort_checker=abort_checker,
                )
                if state_name == "login_password" and allow_login_password:
                    return state_name
                if state_name == "login_password":
                    raise RuntimeError("邮箱提交后进入登录密码页")
                if state_name in ("password", "otp", "logged_in"):
                    return state_name
        time.sleep(1.0)
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")
```

同时给 `_wait_email_submit_next_state()`、`_click_resend_email_otp()`、`_wait_after_email_otp_submit()`、`_fetch_chatgpt_session()` 增加可选 `abort_checker=None`，并在每个轮询循环开头调用 `_run_abort_checker(abort_checker)`。

- [ ] **Step 4: 让浏览器启动接受显式代理和停止检查**

把 `core/roxy_registration.py` 的启动入口改为：

```python
def _open_roxy_registration_browser(
    client: RoxyBrowserClient,
    *,
    device_id: str | None = None,
    proxy: str | None = None,
    stop_checker=None,
) -> tuple[RoxyOpenResult, object]:
    account_device_id = str(device_id or "").strip() or str(uuid.uuid4())
    check_stop = stop_checker or _check_manual_stop
    check_stop()
    with _ROXY_STARTUP_LOCK:
        max_attempts = max(1, int(getattr(_cfg, "ROXY_STARTUP_MAX_ATTEMPTS", 2) or 2))
        retry_delay = max(0.0, float(getattr(_cfg, "ROXY_STARTUP_RETRY_DELAY", 2) or 0))
        for attempt in range(1, max_attempts + 1):
            opened = None
            driver = None
            try:
                check_stop()
                opened = client.open_profile(proxy_url=proxy)
                driver = _build_driver(opened)
                _center_browser_window(driver)
                driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
                install_account_device_id(driver, account_device_id)
                opened.account_device_id = account_device_id
                driver.get("https://chatgpt.com/auth/login")
                human_delay("navigate")
                _maybe_accept(driver)
                _wait_for_email_input_ready(driver, timeout=25)
                return opened, driver
            except BaseException as exc:
                if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
                    try:
                        driver.quit()
                    except Exception:
                        pass
                if opened is not None and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
                    client.cleanup_profile(opened)
                should_retry = (
                    attempt < max_attempts
                    and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN)
                    and _should_retry_roxy_startup(exc)
                )
                if not should_retry:
                    raise
                if retry_delay:
                    time.sleep(retry_delay)
                check_stop()
        raise RuntimeError("Roxy 启动重试已耗尽")
```

更新测试 fake client 的 `open_profile` 签名为 `def open_profile(self, proxy_url=None)`，并断言传入保存代理。

- [ ] **Step 5: 运行共享能力测试**

Run:

```powershell
python -m pytest tests/test_roxy_registration_proxy.py tests/test_roxy_startup_gate.py tests/test_roxy_email_submit.py tests/test_roxy_otp_transition.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交共享能力**

```powershell
git add core/roxybrowser_client.py core/roxy_registration.py tests/test_roxy_registration_proxy.py tests/test_roxy_startup_gate.py tests/test_roxy_email_submit.py tests/test_roxy_otp_transition.py
git commit -m "feat: support reusable Roxy login recovery"
```

---

### Task 2: 建立补 AT 数据库状态机

**Files:**
- Create: `tests/test_access_token_recovery_db.py`
- Modify: `core/db.py:26-32,626-645,848-1052,1356-1435`

- [ ] **Step 1: 写数据库状态机失败测试**

创建 `tests/test_access_token_recovery_db.py`：

```python
# -*- coding: utf-8 -*-
import copy
import json
import unittest
from unittest.mock import patch

from core import db


class AccessTokenRecoveryDbTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"id": 1, "email": "missing@example.com", "access_token": ""},
            {"id": 2, "email": "ready@example.com", "access_token": "TOKEN_READY"},
        ]
        self.saved = []
        self.load_patch = patch.object(db, "_load_accounts", side_effect=lambda: self.accounts)
        self.save_patch = patch.object(
            db,
            "_save_accounts",
            side_effect=lambda rows: self.saved.append(copy.deepcopy(rows)),
        )
        self.load_patch.start()
        self.save_patch.start()

    def tearDown(self):
        self.load_patch.stop()
        self.save_patch.stop()

    def test_claim_only_accepts_missing_token_account(self):
        claimed = db.claim_account_access_token_recovery(
            1,
            trigger="manual",
            log_file="C:/logs/one.log",
        )
        existing = db.claim_account_access_token_recovery(
            2,
            trigger="manual",
            log_file="C:/logs/two.log",
        )

        self.assertTrue(claimed["accepted"])
        self.assertEqual(self.accounts[0]["at_recovery_status"], "queued")
        self.assertTrue(existing["skipped"])
        self.assertEqual(self.accounts[1]["access_token"], "TOKEN_READY")

    def test_second_claim_is_busy(self):
        db.claim_account_access_token_recovery(1, trigger="manual", log_file="C:/logs/one.log")
        second = db.claim_account_access_token_recovery(1, trigger="manual_bulk", log_file="C:/logs/two.log")

        self.assertFalse(second["accepted"])
        self.assertTrue(second["busy"])

    def test_success_writes_session_metadata_without_overwriting_existing_token(self):
        db.claim_account_access_token_recovery(1, trigger="manual", log_file="C:/logs/one.log")
        db.mark_account_access_token_recovery_running(1)
        result = db.complete_account_access_token_recovery(
            1,
            session_info={
                "accessToken": "TOKEN_NEW",
                "user": {"id": "user-1", "name": "Recovered User"},
                "account": {"id": "acct-1", "planType": "free"},
                "expires": "2026-08-06T00:00:00Z",
            },
            device_id="device-1",
            proxy_used="http://sid-1:bridge@127.0.0.1:25001",
        )

        self.assertTrue(result["updated"])
        row = self.accounts[0]
        self.assertEqual(row["access_token"], "TOKEN_NEW")
        self.assertEqual(row["user_id"], "user-1")
        self.assertEqual(row["user_name"], "Recovered User")
        self.assertEqual(row["plan_type"], "free")
        self.assertEqual(row["device_id"], "device-1")
        self.assertEqual(row["proxy_used"], "http://sid-1:bridge@127.0.0.1:25001")
        self.assertEqual(row["at_recovery_status"], "success")
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["account"]["id"], "acct-1")

        self.accounts[1]["at_recovery_status"] = "running"
        untouched = db.complete_account_access_token_recovery(
            2,
            session_info={"accessToken": "TOKEN_DIFFERENT"},
            device_id="device-2",
            proxy_used="http://proxy-2",
        )
        self.assertTrue(untouched["already_present"])
        self.assertEqual(self.accounts[1]["access_token"], "TOKEN_READY")

    def test_stop_and_restart_recovery_preserve_account_data(self):
        db.claim_account_access_token_recovery(1, trigger="manual", log_file="C:/logs/one.log")
        stopped = db.request_account_access_token_recovery_stop(1)
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.accounts[0]["at_recovery_status"], "stopped")

        self.accounts[0].update({
            "at_recovery_status": "running",
            "at_recovery_stop_requested": False,
        })
        recovered = db.recover_interrupted_access_token_recoveries()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.accounts[0]["at_recovery_status"], "failed")
        self.assertEqual(self.accounts[0]["access_token"], "")

    def test_lightweight_snapshot_includes_recovery_status_but_not_log_path(self):
        self.accounts[0].update({
            "at_recovery_status": "failed",
            "at_recovery_error": "OTP 超时",
            "at_recovery_log_file": "C:/private/recovery.log",
        })
        item = db.list_account_plan_check_statuses()["items"][0]
        self.assertEqual(item["at_recovery_status"], "failed")
        self.assertEqual(item["at_recovery_error"], "OTP 超时")
        self.assertTrue(item["has_at_recovery_log"])
        self.assertNotIn("at_recovery_log_file", item)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认状态函数缺失**

```powershell
python -m pytest tests/test_access_token_recovery_db.py -q
```

Expected: FAIL，提示 `claim_account_access_token_recovery` 不存在。

- [ ] **Step 3: 实现领取、运行、停止和恢复函数**

在 `core/db.py` 增加 `_AT_RECOVERY_STALE_SECONDS = 1800`，并实现：

```python
def claim_account_access_token_recovery(
    acc_id: int,
    *,
    trigger: str,
    log_file: str,
) -> dict:
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return {"accepted": False, "busy": False, "skipped": True, "error": "账号不存在"}
        if str(row.get("access_token") or "").strip():
            return {"accepted": False, "busy": False, "skipped": True, "error": "账号已有 access_token"}
        current = str(row.get("at_recovery_status") or "")
        if current in {"queued", "running"}:
            return {"accepted": False, "busy": True, "skipped": False, "error": "该账号正在补 AT"}

        now = _now()
        row.update({
            "at_recovery_status": "queued",
            "at_recovery_error": None,
            "at_recovery_trigger": str(trigger or "manual"),
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
            "account_id": int(row.get("id")),
            "email": str(row.get("email") or ""),
        }


def mark_account_access_token_recovery_running(acc_id: int) -> bool:
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("at_recovery_status") != "queued":
            return False
        if bool(row.get("at_recovery_stop_requested")):
            return False
        row["at_recovery_status"] = "running"
        row["at_recovery_started_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def request_account_access_token_recovery_stop(acc_id: int) -> dict:
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return {"stopped": False, "running": False, "error": "账号不存在"}
        status = str(row.get("at_recovery_status") or "")
        if status == "queued":
            row["at_recovery_status"] = "stopped"
            row["at_recovery_error"] = "用户手动停止"
            row["at_recovery_stop_requested"] = True
            row["at_recovery_completed_at"] = _now()
            row["updated_at"] = _now()
            _save_accounts(accounts)
            return {"stopped": True, "running": False, "status": "stopped"}
        if status == "running":
            row["at_recovery_stop_requested"] = True
            row["updated_at"] = _now()
            _save_accounts(accounts)
            return {"stopped": True, "running": True, "status": "running"}
        return {"stopped": False, "running": False, "error": "该账号没有正在执行的补 AT 任务"}


def is_account_access_token_recovery_stop_requested(acc_id: int) -> bool:
    with _LOCK:
        row = next(
            (item for item in _load_accounts() if int(item.get("id") or 0) == int(acc_id)),
            None,
        )
        return bool(row and row.get("at_recovery_stop_requested"))


def fail_account_access_token_recovery(
    acc_id: int,
    *,
    error: str,
    status: str = "failed",
) -> bool:
    if status not in {"failed", "stopped"}:
        raise ValueError("补 AT 失败状态只能是 failed 或 stopped")
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        row["at_recovery_status"] = status
        row["at_recovery_error"] = str(error or "")[:500]
        row["at_recovery_stop_requested"] = status == "stopped"
        row["at_recovery_completed_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_access_token_recoveries() -> int:
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("at_recovery_status") not in {"queued", "running"}:
                continue
            row["at_recovery_status"] = "failed"
            row["at_recovery_error"] = "WebUI 重启导致补 AT 任务中断，请重新执行"
            row["at_recovery_stop_requested"] = False
            row["at_recovery_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered
```

- [ ] **Step 4: 实现成功原子写回**

继续在 `core/db.py` 增加：

```python
def complete_account_access_token_recovery(
    acc_id: int,
    *,
    session_info: dict,
    device_id: str,
    proxy_used: str | None,
) -> dict:
    token = str((session_info or {}).get("accessToken") or "").strip()
    if not token:
        raise ValueError("session_info 缺少 accessToken")
    user = (session_info or {}).get("user") or {}
    account = (session_info or {}).get("account") or {}

    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return {"updated": False, "already_present": False, "error": "账号不存在"}
        if str(row.get("access_token") or "").strip():
            row["at_recovery_status"] = "success"
            row["at_recovery_error"] = None
            row["at_recovery_completed_at"] = _now()
            row["at_recovery_stop_requested"] = False
            row["updated_at"] = _now()
            _save_accounts(accounts)
            return {"updated": False, "already_present": True}

        extra = {}
        try:
            parsed = json.loads(str(row.get("extra_json") or "{}"))
            if isinstance(parsed, dict):
                extra = parsed
        except Exception:
            extra = {}
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
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return {"updated": True, "already_present": False}
```

`_save_accounts()` 已负责同步账号 JSON、账号 TXT、Token TXT 和静态查看页，不再另写一套同步逻辑。

- [ ] **Step 5: 把状态加入轻量快照和 revision**

在 `list_account_plan_check_statuses()` 的 `fields` 中加入：

```python
"at_recovery_status", "at_recovery_error", "at_recovery_trigger",
"at_recovery_queued_at", "at_recovery_started_at", "at_recovery_completed_at",
```

构造 `item` 时加入：

```python
item["has_at_recovery_log"] = bool(str(row.get("at_recovery_log_file") or "").strip())
```

revision payload 加入：

```python
"has_access_token": bool(str(row.get("access_token") or "").strip()),
"at_recovery_status": row.get("at_recovery_status"),
"at_recovery_error": row.get("at_recovery_error"),
```

- [ ] **Step 6: 运行数据库测试**

```powershell
python -m pytest tests/test_access_token_recovery_db.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交数据库状态机**

```powershell
git add core/db.py tests/test_access_token_recovery_db.py
git commit -m "feat: persist access token recovery state"
```

---

### Task 3: 实现网页版登录补 AT 流程

**Files:**
- Create: `core/roxy_access_token_recovery.py`
- Create: `tests/test_roxy_access_token_recovery.py`

- [ ] **Step 1: 写浏览器流程失败测试**

创建 `tests/test_roxy_access_token_recovery.py`，覆盖四个核心行为：

```python
# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import roxy_access_token_recovery as recovery
from core.roxybrowser_client import RoxyOpenResult


class RoxyAccessTokenRecoveryTests(unittest.TestCase):
    def test_login_password_switches_to_email_otp_and_returns_session(self):
        driver = Mock()
        opened = RoxyOpenResult(
            profile_id="profile-1",
            raw={},
            created_by_run=True,
            registration_proxy="http://sid-1:bridge@127.0.0.1:25001",
            account_device_id="device-1",
        )
        with patch.object(recovery, "_open_browser", return_value=(Mock(), opened, driver)), \
             patch.object(recovery, "_submit_email_and_wait_next", return_value="login_password"), \
             patch.object(recovery, "_click_passwordless_signup_if_present", return_value={"ok": True}), \
             patch.object(recovery, "_wait_for_otp_page_or_session", return_value="otp"), \
             patch.object(recovery, "_wait_for_otp_stoppable", return_value="123456"), \
             patch.object(recovery, "_clear_otp_inputs"), \
             patch.object(recovery, "_type_otp"), \
             patch.object(recovery, "_click_continue"), \
             patch.object(recovery, "_wait_after_email_otp_submit", return_value="accepted"), \
             patch.object(recovery, "_fetch_chatgpt_session", return_value={
                 "accessToken": "TOKEN_NEW",
                 "user": {"id": "user-1"},
             }):
            result = recovery.run_roxy_access_token_recovery(
                email="saved@example.com",
                proxy="http://stored-proxy",
                device_id="device-1",
                should_stop=lambda: False,
            )

        self.assertEqual(result["session_info"]["accessToken"], "TOKEN_NEW")
        self.assertEqual(result["device_id"], "device-1")
        self.assertEqual(
            result["proxy_used"],
            "http://sid-1:bridge@127.0.0.1:25001",
        )

    def test_signup_password_page_is_rejected(self):
        with patch.object(recovery, "_open_browser", return_value=(Mock(), Mock(), Mock())), \
             patch.object(recovery, "_submit_email_and_wait_next", return_value="password"):
            with self.assertRaisesRegex(RuntimeError, "创建账号密码页"):
                recovery.run_roxy_access_token_recovery(
                    email="saved@example.com",
                    proxy=None,
                    device_id="device-1",
                    should_stop=lambda: False,
                )

    def test_phone_verification_aborts_before_session_wait(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/phone-verification"
        with self.assertRaises(recovery.PhoneVerificationRequired):
            recovery._check_abort(driver, lambda: False)

    def test_stop_signal_raises_recovery_stopped(self):
        with self.assertRaises(recovery.AccessTokenRecoveryStopped):
            recovery._check_abort(Mock(), lambda: True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认模块缺失**

```powershell
python -m pytest tests/test_roxy_access_token_recovery.py -q
```

Expected: FAIL，提示无法导入 `core.roxy_access_token_recovery`。

- [ ] **Step 3: 创建完整恢复模块**

创建 `core/roxy_access_token_recovery.py`：

```python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from typing import Callable

from config import roxybrowser as roxy_cfg
from core.email_provider import wait_for_otp
from core.roxy_registration import (
    _clear_otp_inputs,
    _click_continue,
    _click_passwordless_signup_if_present,
    _click_resend_email_otp,
    _email_otp_page_state,
    _fetch_chatgpt_session,
    _has_access_token,
    _is_email_verification_page,
    _open_roxy_registration_browser,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
)
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)


class AccessTokenRecoveryStopped(RuntimeError):
    pass


class PhoneVerificationRequired(RuntimeError):
    pass


def _is_phone_verification_page(driver) -> bool:
    try:
        state = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', autocomplete: el.getAttribute('autocomplete') || ''
        }));
        const forms = [...document.querySelectorAll('form')].map(f => f.getAttribute('action') || '');
        return {url: location.href, inputs, forms, text: (document.body?.innerText || '').slice(0, 1200)};
        """) or {}
    except Exception:
        state = {"url": str(getattr(driver, "current_url", "") or "")}
    if not isinstance(state, dict):
        state = {"url": str(getattr(driver, "current_url", "") or "")}
    url = str(state.get("url") or "").lower()
    attrs = " ".join(
        " ".join(str(item.get(key) or "") for key in ("type", "name", "id", "autocomplete"))
        for item in (state.get("inputs") or [])
    ).lower()
    forms = " ".join(str(value or "") for value in (state.get("forms") or [])).lower()
    return (
        "phone-verification" in url
        or "add-phone" in url
        or "phone-verification" in forms
        or "add-phone" in forms
        or "type tel" in attrs
        or "autocomplete tel" in attrs
    )


def _check_abort(driver, should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise AccessTokenRecoveryStopped("用户手动停止")
    if _is_phone_verification_page(driver):
        raise PhoneVerificationRequired("网页版登录要求手机号验证，本任务已停止")


def _wait_for_otp_page_or_session(
    driver,
    should_stop: Callable[[], bool],
    timeout: int = 30,
) -> str:
    end = time.time() + timeout
    while time.time() < end:
        _check_abort(driver, should_stop)
        if _has_access_token(driver):
            return "logged_in"
        if _is_email_verification_page(driver):
            return "otp"
        time.sleep(0.5)
    raise RuntimeError("点击一次性验证码登录后未进入验证码页")


def _is_otp_timeout(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(value in text for value in ("timeout", "timed out", "超时", "未收到", "no otp"))


def _wait_for_otp_stoppable(
    email: str,
    *,
    after_ts: float,
    should_stop: Callable[[], bool],
    timeout: int = 180,
) -> str:
    end = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < end:
        if should_stop():
            raise AccessTokenRecoveryStopped("用户手动停止")
        remaining = max(1, int(end - time.time()))
        try:
            return wait_for_otp(
                email,
                after_ts=after_ts,
                max_wait=min(10, remaining),
                poll_interval=2,
            )
        except Exception as exc:
            last_exc = exc
            if not _is_otp_timeout(exc):
                raise
    raise RuntimeError(f"等待邮箱验证码超时: {last_exc}")


def _open_browser(
    *,
    proxy: str | None,
    device_id: str,
    should_stop: Callable[[], bool],
):
    client = RoxyBrowserClient()

    def stop_checker() -> None:
        if should_stop():
            raise AccessTokenRecoveryStopped("用户手动停止")

    opened, driver = _open_roxy_registration_browser(
        client,
        device_id=device_id,
        proxy=proxy,
        stop_checker=stop_checker,
    )
    return client, opened, driver


def run_roxy_access_token_recovery(
    *,
    email: str,
    proxy: str | None,
    device_id: str,
    should_stop: Callable[[], bool],
) -> dict:
    client = None
    opened = None
    driver = None
    try:
        client, opened, driver = _open_browser(
            proxy=proxy,
            device_id=device_id,
            should_stop=should_stop,
        )
        abort_checker = lambda: _check_abort(driver, should_stop)
        otp_after_ts = time.time()
        state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            allow_login_password=True,
            abort_checker=abort_checker,
        )
        if state == "password":
            raise RuntimeError("已有账号登录进入创建账号密码页，已停止以避免重新注册")
        if state == "login_password":
            clicked = _click_passwordless_signup_if_present(driver)
            if not clicked.get("ok"):
                raise RuntimeError("登录密码页没有可用的一次性验证码登录入口")
            state = _wait_for_otp_page_or_session(driver, should_stop)

        if state != "logged_in":
            for attempt in range(1, 4):
                abort_checker()
                code = _wait_for_otp_stoppable(
                    email,
                    after_ts=otp_after_ts,
                    should_stop=should_stop,
                )
                _clear_otp_inputs(driver)
                _type_otp(driver, code)
                _click_continue(driver)
                outcome = _wait_after_email_otp_submit(
                    driver,
                    timeout=30,
                    abort_checker=abort_checker,
                )
                if outcome == "accepted":
                    break
                if outcome == "account_deactivated":
                    raise RuntimeError("OpenAI 账号已删除或停用: account_deactivated")
                if attempt >= 3:
                    raise RuntimeError("邮箱验证码连续错误或过期，已达到最大重试次数")
                otp_after_ts = time.time()
                resend = _click_resend_email_otp(
                    driver,
                    timeout=25,
                    abort_checker=abort_checker,
                )
                if resend.get("advanced"):
                    break

        session_info = _fetch_chatgpt_session(
            driver,
            timeout=120,
            abort_checker=abort_checker,
        )
        if not str(session_info.get("accessToken") or "").strip():
            raise RuntimeError("/api/auth/session 未返回 accessToken")
        return {
            "session_info": session_info,
            "device_id": str(opened.account_device_id or device_id),
            "proxy_used": opened.registration_proxy or proxy,
            "profile_id": opened.profile_id,
        }
    finally:
        if driver is not None and not bool(roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if client is not None and opened is not None and not bool(roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                client.cleanup_profile(opened)
            except Exception:
                logger.exception("[补AT] 清理 Roxy 环境失败: profile=%s", opened.profile_id)
```

- [ ] **Step 4: 运行浏览器流程测试**

```powershell
python -m pytest tests/test_roxy_access_token_recovery.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交浏览器流程**

```powershell
git add core/roxy_access_token_recovery.py tests/test_roxy_access_token_recovery.py
git commit -m "feat: recover access tokens through web login"
```

---

### Task 4: 实现后台队列、日志和停止

**Files:**
- Create: `core/access_token_recovery_service.py`
- Create: `tests/test_access_token_recovery_service.py`

- [ ] **Step 1: 写服务失败测试**

创建 `tests/test_access_token_recovery_service.py`，测试：

```python
# -*- coding: utf-8 -*-
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from core import access_token_recovery_service as service


class ImmediateExecutor:
    def submit(self, fn, **kwargs):
        future = Future()
        future.set_result(fn(**kwargs))
        return future


class AccessTokenRecoveryServiceTests(unittest.TestCase):
    def test_worker_uses_saved_proxy_and_generates_missing_device(self):
        account = {
            "id": 7,
            "email": "missing@example.com",
            "access_token": "",
            "proxy_used": "http://saved-proxy",
            "device_id": "",
        }
        with patch.object(service.db, "get_account", return_value=account), \
             patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
             patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
             patch.object(service, "run_roxy_access_token_recovery", return_value={
                 "session_info": {"accessToken": "TOKEN_NEW"},
                 "device_id": "generated-device",
                 "proxy_used": "http://saved-proxy",
             }) as run, \
             patch.object(service.db, "complete_account_access_token_recovery", return_value={
                 "updated": True,
                 "already_present": False,
             }) as complete:
            result = service._run_recovery(account_id=7, trigger="manual")

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.kwargs["proxy"], "http://saved-proxy")
        self.assertTrue(run.call_args.kwargs["device_id"])
        complete.assert_called_once()

    def test_failure_is_sanitized_before_persisting(self):
        account = {
            "id": 8,
            "email": "missing@example.com",
            "access_token": "",
            "proxy_used": "http://user:secret@proxy.example:8080",
            "device_id": "device-8",
        }
        with patch.object(service.db, "get_account", return_value=account), \
             patch.object(service.db, "mark_account_access_token_recovery_running", return_value=True), \
             patch.object(service.db, "is_account_access_token_recovery_stop_requested", return_value=False), \
             patch.object(service, "run_roxy_access_token_recovery", side_effect=RuntimeError(
                 "authorization=Bearer eyJhbGciOi.secret.signature via http://user:secret@proxy.example:8080"
             )), \
             patch.object(service.db, "fail_account_access_token_recovery") as fail:
            result = service._run_recovery(account_id=8, trigger="manual")

        self.assertFalse(result["ok"])
        persisted = fail.call_args.kwargs["error"]
        self.assertNotIn("eyJhbGciOi", persisted)
        self.assertNotIn("user:secret", persisted)

    def test_stop_sets_event_and_database_flag(self):
        with patch.object(service.db, "request_account_access_token_recovery_stop", return_value={
            "stopped": True,
            "running": True,
            "status": "running",
        }):
            result = service.request_stop(9)
        self.assertTrue(result["stopped"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认模块缺失**

```powershell
python -m pytest tests/test_access_token_recovery_service.py -q
```

Expected: FAIL，提示无法导入服务模块。

- [ ] **Step 3: 实现服务核心**

创建 `core/access_token_recovery_service.py`，包含以下接口和行为：

```python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import proxy as proxy_cfg
from core import db
from core.roxy_access_token_recovery import (
    AccessTokenRecoveryStopped,
    run_roxy_access_token_recovery,
)

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_WORKERS = max(1, min(16, int(getattr(proxy_cfg, "PLAN_CHECK_WORKERS", 3) or 3)))
_QUEUE_LIMIT = max(_WORKERS, min(5000, int(getattr(proxy_cfg, "PLAN_CHECK_QUEUE_LIMIT", 500) or 500)))
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="access-token-recovery")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_EVENTS_LOCK = threading.Lock()
_STOP_EVENTS: dict[int, threading.Event] = {}


class _CurrentThreadOnly(logging.Filter):
    def __init__(self):
        super().__init__()
        self.thread_id = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return int(record.thread) == self.thread_id


class _RecoveryLogContext:
    def __init__(self, path: str):
        self.path = Path(path)
        self.handler = logging.FileHandler(self.path, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.handler.addFilter(_CurrentThreadOnly())

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        logging.getLogger().removeHandler(self.handler)
        self.handler.close()


def _sanitize_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"(?i)(https?://)([^/@:\s]+):([^/@\s]+)@", r"\1***:***@", text)
    text = re.sub(r"(?i)\b(authorization|access[_-]?token|token)\b(\s*[=:]\s*)([^\s,;]+)", r"\1\2[redacted]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted-jwt]", text)
    return text[:500]


def _event_for(account_id: int) -> threading.Event:
    with _EVENTS_LOCK:
        return _STOP_EVENTS.setdefault(int(account_id), threading.Event())


def _should_stop(account_id: int) -> bool:
    with _EVENTS_LOCK:
        event = _STOP_EVENTS.get(int(account_id))
    return bool(event and event.is_set()) or db.is_account_access_token_recovery_stop_requested(account_id)


def _run_recovery(*, account_id: int, trigger: str) -> dict:
    try:
        if not db.mark_account_access_token_recovery_running(account_id):
            account = db.get_account(account_id) or {}
            if account.get("at_recovery_status") == "stopped":
                return {"ok": False, "status": "stopped", "error": "用户手动停止"}
            return {"ok": False, "status": "failed", "error": "账号已删除或补 AT 状态已重置"}

        account = db.get_account(account_id)
        if not account:
            raise RuntimeError("账号不存在")
        if str(account.get("access_token") or "").strip():
            complete = db.complete_account_access_token_recovery(
                account_id,
                session_info={"accessToken": str(account.get("access_token"))},
                device_id=str(account.get("device_id") or ""),
                proxy_used=account.get("proxy_used"),
            )
            return {"ok": True, "status": "success", **complete}

        proxy = str(account.get("proxy_used") or "").strip() or str(proxy_cfg.pick_proxy() or "").strip() or None
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
        )
        return {"ok": True, "status": "success", **persisted}
    except AccessTokenRecoveryStopped as exc:
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(account_id, error=error, status="stopped")
        return {"ok": False, "status": "stopped", "error": error}
    except Exception as exc:
        error = _sanitize_error(exc)
        db.fail_account_access_token_recovery(account_id, error=error, status="failed")
        logger.exception("[补AT] 账号恢复失败: account_id=%s", account_id)
        return {"ok": False, "status": "failed", "error": error}


def _run_recovery_with_log(*, account_id: int, trigger: str, log_file: str) -> dict:
    try:
        with _RecoveryLogContext(log_file):
            logger.info("[补AT] 开始: account_id=%s trigger=%s", account_id, trigger)
            result = _run_recovery(account_id=account_id, trigger=trigger)
            logger.info("[补AT] 完成: account_id=%s status=%s", account_id, result.get("status"))
            return result
    finally:
        with _EVENTS_LOCK:
            _STOP_EVENTS.pop(int(account_id), None)
        _QUEUE_SLOTS.release()


def enqueue_account_access_token_recovery(
    *,
    account_id: int,
    trigger: str = "manual",
) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "skipped": False, "error": "补 AT 队列已满"}
    log_file = str(_LOG_DIR / f"at-recovery-{int(account_id)}-{uuid.uuid4().hex}.log")
    claim = db.claim_account_access_token_recovery(
        int(account_id),
        trigger=trigger,
        log_file=log_file,
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


def request_stop(account_id: int) -> dict:
    result = db.request_account_access_token_recovery_stop(int(account_id))
    if result.get("stopped"):
        _event_for(int(account_id)).set()
    return result


def request_stop_bulk(account_ids: list[int]) -> dict:
    stopped = []
    skipped = []
    seen = set()
    for raw in account_ids:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            skipped.append({"id": raw, "reason": "ID 非法"})
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        result = request_stop(account_id)
        if result.get("stopped"):
            stopped.append({"id": account_id, **result})
        else:
            skipped.append({"id": account_id, "reason": result.get("error") or "未停止"})
    return {"stopped": stopped, "stopped_count": len(stopped), "skipped": skipped, "skipped_count": len(skipped)}


def read_log(account_id: int, max_bytes: int = 50_000) -> str:
    account = db.get_account(int(account_id))
    path = Path(str((account or {}).get("at_recovery_log_file") or ""))
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max(1, int(max_bytes))))
        return handle.read().decode("utf-8", errors="replace")


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
```

- [ ] **Step 4: 运行服务测试**

```powershell
python -m pytest tests/test_access_token_recovery_service.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交服务模块**

```powershell
git add core/access_token_recovery_service.py tests/test_access_token_recovery_service.py
git commit -m "feat: queue and stop access token recovery"
```

---

### Task 5: 接入 Flask API 和启动恢复

**Files:**
- Create: `tests/test_webui_access_token_recovery.py`
- Modify: `webui/app.py:22,146-201,267-327,371-409,586-700`

- [ ] **Step 1: 写 API 失败测试**

创建 `tests/test_webui_access_token_recovery.py`：

```python
# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiAccessTokenRecoveryTests(unittest.TestCase):
    def setUp(self):
        with patch("webui.app.db.recover_interrupted_access_token_recoveries", return_value=0), \
             patch("webui.app.db.recover_interrupted_extract_links", return_value={
                 "failed_count": 0,
                 "kakao_batches": [],
             }), \
             patch("webui.app.extract_link_service.resume_interrupted_kakao_batches", return_value={
                 "resumed_batches": 0,
                 "failed_batches": 0,
             }):
            self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_single_recovery_returns_202(self):
        with patch("webui.app.access_token_recovery_service.enqueue_account_access_token_recovery", return_value={
            "accepted": True,
            "busy": False,
            "skipped": False,
            "account_id": 7,
            "email": "missing@example.com",
        }) as enqueue:
            response = self.client.post("/api/accounts/recover-access-token", json={"account_id": 7})

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once_with(account_id=7, trigger="manual")

    def test_bulk_recovery_classifies_started_busy_and_skipped(self):
        results = [
            {"accepted": True, "busy": False, "skipped": False, "account_id": 1},
            {"accepted": False, "busy": True, "skipped": False, "error": "正在补 AT"},
            {"accepted": False, "busy": False, "skipped": True, "error": "已有 access_token"},
        ]
        with patch(
            "webui.app.access_token_recovery_service.enqueue_account_access_token_recovery",
            side_effect=results,
        ):
            response = self.client.post(
                "/api/accounts/recover-access-token-bulk",
                json={"account_ids": [1, 2, 3]},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["busy_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)

    def test_stop_bulk_and_log(self):
        with patch("webui.app.access_token_recovery_service.request_stop_bulk", return_value={
            "stopped": [{"id": 1}],
            "stopped_count": 1,
            "skipped": [],
            "skipped_count": 0,
        }):
            stopped = self.client.post(
                "/api/accounts/recover-access-token/stop-bulk",
                json={"account_ids": [1]},
            )
        with patch("webui.app.access_token_recovery_service.read_log", return_value="recovery log"):
            log = self.client.get("/api/accounts/recover-access-token/1/log")

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(log.get_json()["log"], "recovery log")

    def test_compact_account_exposes_status_without_log_path(self):
        account = {
            "id": 1,
            "email": "missing@example.com",
            "access_token": "",
            "at_recovery_status": "failed",
            "at_recovery_error": "OTP 超时",
            "at_recovery_log_file": "C:/private/recovery.log",
        }
        with patch("webui.app.db.list_accounts_page", return_value={
            "items": [account],
            "total": 1,
            "offset": 0,
            "limit": 50,
            "revision": "1",
        }):
            response = self.client.get("/api/accounts?paged=1")

        item = response.get_json()["items"][0]
        self.assertEqual(item["at_recovery_status"], "failed")
        self.assertTrue(item["has_at_recovery_log"])
        self.assertNotIn("at_recovery_log_file", item)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认 API 缺失**

```powershell
python -m pytest tests/test_webui_access_token_recovery.py -q
```

Expected: FAIL，提示服务未导入或路由返回 404。

- [ ] **Step 3: 导入服务并恢复中断状态**

在 `webui/app.py` 顶部导入中加入：

```python
from core import access_token_recovery_service
```

在 `create_app()` 启动恢复区域加入：

```python
recovered_access_tokens = db.recover_interrupted_access_token_recoveries()
if recovered_access_tokens:
    logger.warning("已恢复 %s 个因 WebUI 重启中断的补 AT 状态", recovered_access_tokens)
```

- [ ] **Step 4: 加入列表字段**

在 `_compact_account_for_list()` 固定字段和可选字段处理中加入：

```python
for key in (
    "at_recovery_status",
    "at_recovery_error",
    "at_recovery_trigger",
    "at_recovery_queued_at",
    "at_recovery_started_at",
    "at_recovery_completed_at",
):
    value = row.get(key)
    if value is not None and value != "":
        out[key] = value
out["has_at_recovery_log"] = bool(str(row.get("at_recovery_log_file") or "").strip())
```

- [ ] **Step 5: 实现单个、批量、停止和日志 API**

在账号路由区域加入：

```python
@app.post("/api/accounts/recover-access-token")
def api_account_recover_access_token():
    data = request.get_json(silent=True) or {}
    raw_id = data.get("account_id") or data.get("id")
    try:
        account_id = int(raw_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "account_id 无效"}), 400
    result = access_token_recovery_service.enqueue_account_access_token_recovery(
        account_id=account_id,
        trigger="manual",
    )
    result = {key: value for key, value in result.items() if key != "future"}
    if result.get("accepted"):
        return jsonify({"ok": True, "started": True, **result}), 202
    if result.get("busy"):
        return jsonify({"ok": False, **result}), 409
    if result.get("skipped"):
        return jsonify({"ok": True, **result}), 200
    return jsonify({"ok": False, **result}), 503


@app.post("/api/accounts/recover-access-token-bulk")
def api_accounts_recover_access_token_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get("account_ids") or data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
    if len(ids) > 500:
        return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400
    started, busy, failed, skipped = [], [], [], []
    seen = set()
    for raw in ids:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            skipped.append({"id": raw, "reason": "ID 非法"})
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        result = access_token_recovery_service.enqueue_account_access_token_recovery(
            account_id=account_id,
            trigger="manual_bulk",
        )
        item = {"id": account_id, **{key: value for key, value in result.items() if key != "future"}}
        if result.get("accepted"):
            started.append(item)
        elif result.get("busy"):
            busy.append(item)
        elif result.get("skipped"):
            skipped.append({"id": account_id, "reason": result.get("error") or "已跳过"})
        else:
            failed.append(item)
    return jsonify({
        "ok": True,
        "started": started,
        "started_count": len(started),
        "busy": busy,
        "busy_count": len(busy),
        "failed": failed,
        "failed_count": len(failed),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }), 202


@app.post("/api/accounts/recover-access-token/<int:account_id>/stop")
def api_account_recover_access_token_stop(account_id: int):
    result = access_token_recovery_service.request_stop(account_id)
    return jsonify({"ok": bool(result.get("stopped")), **result}), 200 if result.get("stopped") else 409


@app.post("/api/accounts/recover-access-token/stop-bulk")
def api_accounts_recover_access_token_stop_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get("account_ids") or data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
    result = access_token_recovery_service.request_stop_bulk(ids)
    return jsonify({"ok": True, **result})


@app.get("/api/accounts/recover-access-token/<int:account_id>/log")
def api_account_recover_access_token_log(account_id: int):
    account = db.get_account(account_id)
    if not account:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    return jsonify({
        "ok": True,
        "account_id": account_id,
        "email": account.get("email"),
        "status": account.get("at_recovery_status"),
        "running": account.get("at_recovery_status") in {"queued", "running"},
        "log": access_token_recovery_service.read_log(account_id),
    })
```

- [ ] **Step 6: 运行 API 测试**

```powershell
python -m pytest tests/test_webui_access_token_recovery.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 API**

```powershell
git add webui/app.py tests/test_webui_access_token_recovery.py
git commit -m "feat: expose access token recovery APIs"
```

---

### Task 6: 实现账号页交互和状态轮询

**Files:**
- Create: `tests/test_webui_access_token_recovery_template.py`
- Modify: `webui/templates/index.html:543-610,1387-1425,1426-1668,1673-1715,1748-1919`

- [ ] **Step 1: 写模板契约失败测试**

创建 `tests/test_webui_access_token_recovery_template.py`：

```python
# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class WebUiAccessTokenRecoveryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_toolbar_has_recover_and_stop_buttons(self):
        self.assertIn('id="btnRecoverSelectedAccessTokens"', self.html)
        self.assertIn('id="btnStopSelectedAccessTokenRecovery"', self.html)

    def test_account_row_has_single_recover_stop_and_log_actions(self):
        self.assertIn("data-at-recover", self.html)
        self.assertIn("data-at-recovery-stop", self.html)
        self.assertIn("data-at-recovery-log", self.html)

    def test_polling_merges_recovery_state(self):
        self.assertIn("wasRecovering", self.html)
        self.assertIn("isRecovering", self.html)
        self.assertIn("at_recovery_status", self.html)

    def test_bulk_calls_expected_endpoints(self):
        self.assertIn("/api/accounts/recover-access-token-bulk", self.html)
        self.assertIn("/api/accounts/recover-access-token/stop-bulk", self.html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认按钮缺失**

```powershell
python -m pytest tests/test_webui_access_token_recovery_template.py -q
```

Expected: FAIL。

- [ ] **Step 3: 增加工具栏按钮和日志弹窗**

在账号工具栏增加：

```html
<div class="btn-group">
  <span class="btn-group-title">Access Token</span>
  <button class="btn primary" id="btnRecoverSelectedAccessTokens" disabled title="给选中的缺失 Access Token 账号自动登录补 AT">补缺失AT</button>
  <button class="btn danger" id="btnStopSelectedAccessTokenRecovery" disabled title="停止选中账号中排队或运行的补 AT 任务">停止补AT</button>
</div>
```

在页面已有模态框区域增加：

```html
<div class="modal hidden" id="atRecoveryLogModal">
  <div class="modal-card">
    <div class="modal-head">
      <h3>补 AT 日志 · <span id="atRecoveryLogEmail"></span></h3>
      <button class="btn" id="btnCloseAtRecoveryLog">关闭</button>
    </div>
    <div class="log" id="atRecoveryLogContent">加载中…</div>
  </div>
</div>
```

- [ ] **Step 4: 增加状态单元格和行操作**

在 `renderAccounts()` 前增加：

```javascript
function _accessTokenCell(r) {
  if (r.has_access_token) return '<span class="mono" title="列表仅显示状态，完整 Token 请点复制">有Token</span>';
  const status = r.at_recovery_status || '';
  const error = r.at_recovery_error || '';
  if (status === 'queued') return '<span class="pill status-running">补AT排队中</span>';
  if (status === 'running') return '<span class="pill status-running">补AT中</span>';
  if (status === 'failed') return `<div class="extract-link-cell"><span class="pill status-failed" title="${esc(error)}">补AT失败</span><div class="extract-link-error">${esc(error)}</div></div>`;
  if (status === 'stopped') return `<span class="pill status-used" title="${esc(error)}">补AT已停止</span>`;
  return '<span class="muted">无Token</span>';
}

function _accessTokenRecoveryAction(r) {
  const status = r.at_recovery_status || '';
  const log = r.has_at_recovery_log
    ? `<button data-at-recovery-log="${esc(r.id)}" title="查看最近一次补 AT 日志">补AT日志</button>`
    : '';
  if (['queued', 'running'].includes(status)) {
    return `<button class="danger" data-at-recovery-stop="${esc(r.id)}" title="停止该账号补 AT">停止补AT</button>${log}`;
  }
  if (r.has_access_token) return log;
  return `<button class="primary" data-at-recover="${esc(r.id)}" title="通过网页版登录和邮箱验证码补 Access Token">补AT</button>${log}`;
}
```

把 Token 单元格替换为：

```javascript
<td>${_accessTokenCell(r)}</td>
```

把第一组账号操作替换为：

```javascript
<div class="account-action-group">
  <button class="primary" data-account-copy-secret="access_token" data-account-id="${esc(r.id)}" ${r.has_access_token ? '' : 'disabled'}>复制Token</button>
  ${_accessTokenRecoveryAction(r)}
  <button class="good" data-account-copy-secret="copy_line" data-account-id="${esc(r.id)}">复制整行</button>
</div>
```

- [ ] **Step 5: 增加单个和批量操作函数**

在账号 JavaScript 区域加入：

```javascript
async function recoverOneAccessToken(id, btn) {
  const account = ACCOUNTS.find(item => Number(item.id) === Number(id));
  if (!account || account.has_access_token) { showToast('账号已有 Token 或不存在'); return; }
  if (!confirm(`确定为该账号自动补 AT 吗？\n\n${account.email || ('#' + id)}\n\n只登录网页版并读取邮箱验证码，不调用接码平台。`)) return;
  btn.disabled = true;
  try {
    const result = await api('/api/accounts/recover-access-token', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({account_id: id}),
    });
    showToast(result.started ? '补 AT 已加入后台队列' : (result.error || '账号已跳过'));
    await pollAccountPlanStatuses();
  } catch (error) {
    showToast('补 AT 提交失败: ' + error.message);
    await pollAccountPlanStatuses();
  }
}

async function stopOneAccessTokenRecovery(id, btn) {
  if (!confirm('确定停止该账号的补 AT 任务吗？')) return;
  btn.disabled = true;
  try {
    const result = await api(`/api/accounts/recover-access-token/${encodeURIComponent(id)}/stop`, {method: 'POST'});
    showToast(result.running ? '已发送停止信号' : '已停止排队任务');
    await pollAccountPlanStatuses();
  } catch (error) {
    showToast('停止补 AT 失败: ' + error.message);
  }
}

async function recoverSelectedAccessTokens() {
  const ids = Array.from(ACCOUNT_SELECTED).map(Number);
  if (!ids.length) { showToast('请先选择账号'); return; }
  const selected = ids.map(id => ACCOUNTS.find(item => Number(item.id) === id)).filter(Boolean);
  const eligible = selected.filter(item => !item.has_access_token && !['queued', 'running'].includes(item.at_recovery_status));
  const skipped = selected.length - eligible.length;
  if (!eligible.length) { showToast('选中账号没有可补 AT 项'); return; }
  if (!confirm(`确定批量补 AT ${eligible.length} 个账号吗？\n\n已有 Token 或运行中的 ${skipped} 个账号会跳过；流程只使用网页版登录和邮箱验证码。`)) return;
  const result = await api('/api/accounts/recover-access-token-bulk', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({account_ids: ids}),
  });
  showToast(`已开始 ${result.started_count || 0} 个，忙碌 ${result.busy_count || 0} 个，跳过 ${result.skipped_count || 0} 个`);
  await pollAccountPlanStatuses();
}

async function stopSelectedAccessTokenRecovery() {
  const selected = Array.from(ACCOUNT_SELECTED)
    .map(id => ACCOUNTS.find(item => Number(item.id) === Number(id)))
    .filter(item => item && ['queued', 'running'].includes(item.at_recovery_status));
  if (!selected.length) { showToast('选中账号里没有正在补 AT 的任务'); return; }
  if (!confirm(`确定停止选中的 ${selected.length} 个补 AT 任务吗？`)) return;
  const result = await api('/api/accounts/recover-access-token/stop-bulk', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({account_ids: selected.map(item => item.id)}),
  });
  showToast(`已停止 ${result.stopped_count || 0} 个，跳过 ${result.skipped_count || 0} 个`);
  await pollAccountPlanStatuses();
}
```

- [ ] **Step 6: 增加日志轮询和事件委托**

```javascript
let atRecoveryLogAccountId = null;
let atRecoveryLogTimer = null;

async function pollAccessTokenRecoveryLog() {
  if (!atRecoveryLogAccountId) return;
  try {
    const result = await api(`/api/accounts/recover-access-token/${encodeURIComponent(atRecoveryLogAccountId)}/log`);
    $('#atRecoveryLogEmail').textContent = result.email || `#${atRecoveryLogAccountId}`;
    $('#atRecoveryLogContent').textContent = result.log || '暂无日志';
    if (!result.running && atRecoveryLogTimer) {
      clearInterval(atRecoveryLogTimer);
      atRecoveryLogTimer = null;
      await loadAccounts();
    }
  } catch (error) {
    $('#atRecoveryLogContent').textContent = '日志读取失败: ' + error.message;
  }
}

function openAccessTokenRecoveryLog(id) {
  atRecoveryLogAccountId = Number(id);
  $('#atRecoveryLogModal').classList.remove('hidden');
  pollAccessTokenRecoveryLog();
  if (atRecoveryLogTimer) clearInterval(atRecoveryLogTimer);
  atRecoveryLogTimer = setInterval(pollAccessTokenRecoveryLog, 2000);
}

function closeAccessTokenRecoveryLog() {
  if (atRecoveryLogTimer) clearInterval(atRecoveryLogTimer);
  atRecoveryLogTimer = null;
  atRecoveryLogAccountId = null;
  $('#atRecoveryLogModal').classList.add('hidden');
}
```

在账号点击事件委托中加入 `data-at-recover`、`data-at-recovery-stop`、`data-at-recovery-log` 分支；绑定：

```javascript
$('#btnRecoverSelectedAccessTokens').addEventListener('click', recoverSelectedAccessTokens);
$('#btnStopSelectedAccessTokenRecovery').addEventListener('click', stopSelectedAccessTokenRecovery);
$('#btnCloseAtRecoveryLog').addEventListener('click', closeAccessTokenRecoveryLog);
```

- [ ] **Step 7: 把补 AT 状态合并到轮询和选中按钮状态**

`pollAccountPlanStatuses()` 合并状态时加入：

```javascript
const wasRecovering = ['queued', 'running'].includes(account?.at_recovery_status);
const isRecovering = ['queued', 'running'].includes(item.at_recovery_status);
if (wasRecovering && !isRecovering) needsFullReload = true;
```

`updateAccountSelectionUi()` 增加：

```javascript
const recoverAtBtn = $('#btnRecoverSelectedAccessTokens');
const stopRecoverAtBtn = $('#btnStopSelectedAccessTokenRecovery');
if (recoverAtBtn) recoverAtBtn.disabled = ACCOUNT_SELECTED.size === 0;
if (stopRecoverAtBtn) stopRecoverAtBtn.disabled = ACCOUNT_SELECTED.size === 0;
```

- [ ] **Step 8: 运行模板和 API 测试**

```powershell
python -m pytest tests/test_webui_access_token_recovery_template.py tests/test_webui_access_token_recovery.py -q
```

Expected: PASS。

- [ ] **Step 9: 提交 WebUI**

```powershell
git add webui/templates/index.html tests/test_webui_access_token_recovery_template.py
git commit -m "feat: add access token recovery controls"
```

---

### Task 7: 文档、回归和真实缺失账号验收

**Files:**
- Modify: `README.md:账号功能说明附近`
- Modify: `docs/superpowers/specs/2026-08-05-missing-access-token-recovery-design.md`

- [ ] **Step 1: 更新 README 操作说明**

在账号功能章节增加：

```markdown
### 给缺失账号补 Access Token

账号页中 `access_token` 为空的账号会显示“补 AT”。补全流程会创建独立 Roxy 环境，优先复用账号注册代理和 `device_id`，通过 ChatGPT 网页登录并从账号原邮箱来源读取一次性验证码。

- 支持单账号和勾选批量补全。
- 已有 Access Token 的账号自动跳过且不会被覆盖。
- 代理或 `device_id` 缺失时使用当前代理配置并生成新的账号设备 ID。
- 出现手机号验证时该账号任务失败，不调用接码平台。
- 成功后同步账号 JSON、账号 TXT、Token TXT 和静态查看页。
- 排队或运行中的任务可停止；账号行可查看最近一次补 AT 日志。
```

- [ ] **Step 2: 运行全部新增和相关回归测试**

```powershell
python -m pytest tests/test_access_token_recovery_db.py tests/test_roxy_access_token_recovery.py tests/test_access_token_recovery_service.py tests/test_webui_access_token_recovery.py tests/test_webui_access_token_recovery_template.py tests/test_roxy_registration_proxy.py tests/test_roxy_startup_gate.py tests/test_roxy_email_submit.py tests/test_roxy_otp_transition.py -q
```

Expected: PASS，无 warning、traceback 或线程泄漏。

- [ ] **Step 3: 运行全量测试**

```powershell
python -m pytest -q
```

Expected: 全部测试 PASS。

- [ ] **Step 4: 检查差异和敏感信息**

```powershell
git diff --check
git status --short
git grep -n -E "eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|Bearer [A-Za-z0-9._-]{40,}" -- core tests webui README.md
```

Expected: `git diff --check` 无输出；grep 不命中真实 Token；`logs/` 保持未跟踪且不加入提交。

- [ ] **Step 5: 枚举本地缺失 AT 账号并备份数据文件**

```powershell
@'
from core import db
rows = db.list_accounts(limit=100000, archived="all")
missing = [row for row in rows if not str(row.get("access_token") or "").strip()]
print("missing_at_count=", len(missing))
for row in missing[:20]:
    print(row.get("id"), row.get("email"), row.get("email_source"), bool(row.get("proxy_used")), bool(row.get("device_id")))
'@ | python -

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath '注册成功的邮箱.json' -Destination "注册成功的邮箱.$stamp.backup.json"
Copy-Item -LiteralPath '注册成功的token.txt' -Destination "注册成功的token.$stamp.backup.txt"
```

Expected: 输出可测试的缺失账号数量；备份文件创建成功。备份文件只用于本机验收，不加入 Git。

- [ ] **Step 6: 通过 WebUI 验收单个补 AT**

1. 启动 WebUI。
2. 在账号页选择一个缺失 AT 的账号，点击“补 AT”。
3. 打开“补AT日志”，确认出现 Roxy 启动、邮箱提交、OTP 和 session 成功记录。
4. 确认账号 Token 列变为“有Token”。
5. 确认 `注册成功的token.txt` 新增对应 Token，但页面和日志不显示完整 Token。

Expected: 单账号状态从 `queued` → `running` → `success`，账号元数据更新。

- [ ] **Step 7: 通过 WebUI 验收批量、停止和失败隔离**

1. 勾选至少两个缺失 AT 的账号，点击“补缺失AT”。
2. 对其中一个排队或运行任务点击“停止补AT”。
3. 确认被停止账号状态为 `stopped`，另一个账号继续运行。
4. 如存在邮箱凭据失效账号，确认其状态为 `failed`，错误摘要和日志可读，其他账号不受影响。

Expected: 批量结果互相隔离；停止不修改原账号 Token 和邮箱池状态。

- [ ] **Step 8: 提交文档和最终验证结果**

```powershell
git add README.md docs/superpowers/specs/2026-08-05-missing-access-token-recovery-design.md
git commit -m "docs: document access token recovery"
```

---

## 完成定义

- 单个与批量补 AT API、按钮和状态轮询工作正常。
- 补全流程只使用网页版登录和邮箱 OTP，未导入或调用 `core.sms_provider`。
- 已有 Token 不被覆盖。
- 保存代理、默认代理和生成设备 ID 三条路径均有测试。
- 排队和运行任务均可停止，进程重启遗留状态可恢复。
- 成功写回同步全部派生账号文件。
- 日志、列表 API 和错误信息不包含完整 Token、代理密码或邮箱取件凭据。
- 新增测试、相关回归测试和全量测试全部通过。
