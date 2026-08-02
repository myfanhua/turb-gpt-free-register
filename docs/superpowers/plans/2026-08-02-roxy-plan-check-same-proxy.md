# Roxy 注册后套餐复检复用同一代理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Roxy 注册、注册后首次套餐检测和两秒 Plus 资格复检复用同一个账号专属韩国代理，同时保持手动检测和无代理注册的现有行为。

**Architecture:** Roxy 客户端在创建临时 Profile 时捕获实际传给 Roxy 的代理 URL，并通过 `RoxyOpenResult` 传递到注册流程。账号落库后，`save_account_data()` 将该代理显式传给 `registration_auto` 套餐查询；`plan_check_service` 已有的首次查询与两秒复检继续复用同一 `proxy` 参数。

**Tech Stack:** Python 3.13、`unittest`、`unittest.mock`、现有 RoxyBrowser 客户端与套餐查询线程池。

---

## 文件结构

- Modify: `core/roxybrowser_client.py` — 捕获并返回本轮 Roxy 注册代理。
- Modify: `core/roxy_registration.py` — 将 Roxy 注册代理保存到账号记录。
- Modify: `core/account_export.py` — 把注册代理传给自动套餐查询队列。
- Create: `tests/test_roxy_registration_proxy.py` — 验证 Roxy 代理捕获。
- Create: `tests/test_account_export_plan_proxy.py` — 验证账号保存后的代理传递。
- Create: `tests/test_plan_check_same_proxy.py` — 验证首次查询和两秒复检使用同一代理。

### Task 1: 捕获 Roxy 创建环境时的实际代理

**Files:**
- Modify: `core/roxybrowser_client.py:20-32`
- Modify: `core/roxybrowser_client.py:412-526`
- Create: `tests/test_roxy_registration_proxy.py`

- [ ] **Step 1: 写入失败测试**

```python
# tests/test_roxy_registration_proxy.py
import unittest
from unittest.mock import patch

from config import roxybrowser as roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient


class RoxyRegistrationProxyTests(unittest.TestCase):
    def test_open_profile_exposes_actual_created_proxy(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        upstream = "http://user-region-KR-sid-abc123-t-3:pass@upstream.example:3000"
        bridge = "http://sid-abc123:bridge@127.0.0.1:25001"
        responses = [
            {"code": 0, "data": {"dirId": "profile-1"}},
            {"code": 0, "data": {"http": "127.0.0.1:9222"}},
        ]
        with patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True), \
             patch.object(roxy_cfg, "ROXY_PROFILE_ID", ""), \
             patch.object(roxy_cfg, "ROXY_CREATE_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy", return_value=upstream), \
             patch("core.roxybrowser_client.prepare_proxy_for_roxy", return_value=bridge), \
             patch.object(client, "request", side_effect=responses):
            opened = client.open_profile()
        self.assertEqual(opened.profile_id, "profile-1")
        self.assertEqual(opened.registration_proxy, bridge)

    def test_configured_profile_keeps_registration_proxy_empty(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        response = {"code": 0, "data": {"http": "127.0.0.1:9222"}}
        with patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False), \
             patch.object(roxy_cfg, "ROXY_PROFILE_ID", "profile-existing"), \
             patch.object(client, "request", return_value=response):
            opened = client.open_profile()
        self.assertIsNone(opened.registration_proxy)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_roxy_registration_proxy.py -q`

Expected: FAIL，`RoxyOpenResult` 没有 `registration_proxy`。

- [ ] **Step 3: 实现最小代理上下文传递**

在 `RoxyOpenResult` 增加字段：

```python
registration_proxy: str | None = None
```

将 `create_profile()` 的返回值改为：

```python
def create_profile(self, payload: dict | None = None) -> tuple[str, str | None]:
    registration_proxy = None
    # 现有 pick_proxy/prepare_proxy_for_roxy 分支中：
    registration_proxy = prepare_proxy_for_roxy(proxy_url)
    body["proxyInfo"] = _proxy_url_to_roxy_info(registration_proxy)
    # 创建成功后：
    return profile_id, registration_proxy
```

在 `open_profile()` 接收并返回字段：

```python
registration_proxy = None
if not pid:
    pid, registration_proxy = self.create_profile()
    created_by_run = True

return RoxyOpenResult(
    pid,
    result,
    debugger_address=debugger_address,
    webdriver_url=webdriver_url,
    ws_endpoint=ws_endpoint,
    created_by_run=created_by_run,
    registration_proxy=registration_proxy,
)
```

保留现有完整创建参数、脱敏日志和错误处理。

- [ ] **Step 4: 运行聚焦测试**

Run: `python -m pytest tests/test_roxy_registration_proxy.py tests/test_proxy_bridge_config.py tests/test_proxy_rotation.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add core/roxybrowser_client.py tests/test_roxy_registration_proxy.py
git commit -m "feat: retain roxy registration proxy"
```

### Task 2: 将注册代理传给自动套餐查询

**Files:**
- Modify: `core/roxy_registration.py:1668-1696`
- Modify: `core/account_export.py:382-451`
- Create: `tests/test_account_export_plan_proxy.py`

- [ ] **Step 1: 写入失败测试**

```python
# tests/test_account_export_plan_proxy.py
import unittest
from unittest.mock import patch

from core.account_export import save_account_data


class AccountExportPlanProxyTests(unittest.TestCase):
    def test_registration_auto_plan_check_receives_saved_proxy(self):
        registration_proxy = "http://sid-abc123:bridge@127.0.0.1:25001"
        with patch("core.db.insert_account", return_value=42), \
             patch("core.account_export._append_batch_archive", return_value="batch"), \
             patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True}) as enqueue:
            row_id = save_account_data(
                email="user@example.com",
                access_token="token-value",
                proxy_used=registration_proxy,
                extra={"account": {"planType": "free"}},
            )
        self.assertEqual(row_id, 42)
        enqueue.assert_called_once_with(
            account_id=42,
            email="user@example.com",
            access_token="token-value",
            trigger="registration_auto",
            proxy=registration_proxy,
        )

    def test_registration_auto_keeps_default_route_when_proxy_missing(self):
        with patch("core.db.insert_account", return_value=43), \
             patch("core.account_export._append_batch_archive", return_value="batch"), \
             patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True}) as enqueue:
            save_account_data(email="direct@example.com", access_token="token-value", proxy_used=None, extra={})
        enqueue.assert_called_once_with(
            account_id=43,
            email="direct@example.com",
            access_token="token-value",
            trigger="registration_auto",
            proxy=None,
        )
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_account_export_plan_proxy.py -q`

Expected: FAIL，当前自动查询调用缺少 `proxy` 参数。

- [ ] **Step 3: 实现代理传递**

修改 `core/account_export.py`：

```python
queued = enqueue_account_plan_check(
    account_id=row_id,
    email=email,
    access_token=access_token,
    trigger="registration_auto",
    proxy=proxy_used,
)
```

修改 `core/roxy_registration.py`：

```python
registration_proxy = opened.registration_proxy or proxy or None
account_id = save_account_data(
    email=email,
    access_token=access_token,
    totp_secret=totp_secret,
    email_source=resolve_email_source(email),
    proxy_used=registration_proxy,
    batch_dir=batch_dir,
    extra={
        "user": session_info.get("user"),
        "account": session_info.get("account"),
        "expires": session_info.get("expires"),
        "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
        "registration_password": openai_password,
        "codex": codex_result,
    },
)
```

Roxy捕获代理优先，显式 `proxy` 参数作为兼容回退。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_account_export_plan_proxy.py -q`

Expected: 2 tests PASS。

- [ ] **Step 5: 提交**

```powershell
git add core/account_export.py core/roxy_registration.py tests/test_account_export_plan_proxy.py
git commit -m "fix: reuse registration proxy for plan checks"
```

### Task 3: 锁定首次查询和两秒复检使用同一代理

**Files:**
- Create: `tests/test_plan_check_same_proxy.py`
- Verify: `core/plan_check_service.py:61-110`

- [ ] **Step 1: 写入回归测试**

```python
# tests/test_plan_check_same_proxy.py
import unittest
from unittest.mock import Mock, call, patch

from core import plan_check_service


class PlanCheckSameProxyTests(unittest.TestCase):
    def test_registration_recheck_reuses_explicit_proxy(self):
        proxy = "http://sid-abc123:bridge@127.0.0.1:25001"
        first = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False}
        second = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": True}
        slots = Mock()
        with patch.object(plan_check_service, "_QUEUE_SLOTS", slots), \
             patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True), \
             patch.object(plan_check_service.db, "update_account_plan_check"), \
             patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(plan_check_service, "_registration_recheck_delay", return_value=2.0), \
             patch.object(plan_check_service.time, "sleep"), \
             patch.object(plan_check_service, "check_account_plan", side_effect=[first, second]) as check:
            result = plan_check_service._run_plan_check(
                account_id=42,
                email="user@example.com",
                access_token="token-value",
                trigger="registration_auto",
                proxy=proxy,
                timezone_offset_min="-",
            )
        self.assertEqual(result, second)
        self.assertEqual(check.call_args_list, [
            call("token-value", proxy=proxy, timezone_offset_min="-"),
            call("token-value", proxy=proxy, timezone_offset_min="-", max_attempts=1),
        ])
        plan_check_service.time.sleep.assert_called_once_with(2.0)
        slots.release.assert_called_once_with()
```

- [ ] **Step 2: 运行测试**

Run: `python -m pytest tests/test_plan_check_same_proxy.py -q`

Expected: PASS；现有实现已复用相同 `proxy`，该测试用于防止回归。

- [ ] **Step 3: 提交**

```powershell
git add tests/test_plan_check_same_proxy.py
git commit -m "test: lock plan recheck to registration proxy"
```

### Task 4: 完整回归验证

**Files:**
- Verify: `core/roxybrowser_client.py`
- Verify: `core/roxy_registration.py`
- Verify: `core/account_export.py`
- Verify: `tests/test_roxy_registration_proxy.py`
- Verify: `tests/test_account_export_plan_proxy.py`
- Verify: `tests/test_plan_check_same_proxy.py`

- [ ] **Step 1: 运行聚焦测试**

Run: `python -m pytest tests/test_roxy_registration_proxy.py tests/test_account_export_plan_proxy.py tests/test_plan_check_same_proxy.py tests/test_proxy_bridge_config.py tests/test_proxy_rotation.py -q`

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整测试套件**

Run: `python -m pytest -q`

Expected: 现有测试和新增测试全部 PASS，无失败。

- [ ] **Step 3: 检查差异和敏感信息**

```powershell
git diff --check
git status --short
git diff -- core/roxybrowser_client.py core/roxy_registration.py core/account_export.py tests/test_roxy_registration_proxy.py tests/test_account_export_plan_proxy.py tests/test_plan_check_same_proxy.py
```

Expected: `git diff --check` 无输出；差异不包含代理密码、API Key 或完整 Access Token。

## 验收结果

- 每个账号仍生成独立 `sid`。
- 注册、首次套餐检测和两秒复检使用完全相同的代理 URL。
- `PLAN_CHECK_REGISTRATION_RECHECK_DELAY=2.0` 保持不变。
- 注册线程没有新增等待步骤。
- 手动检测仍使用全局 `PLAN_CHECK_PROXY`。
- 未配置 Roxy 代理时维持原有自动路由逻辑。
