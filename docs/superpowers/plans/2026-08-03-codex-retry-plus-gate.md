# Codex Retry Plus Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在所有 Codex 补跑路径真正执行邮箱 OTP 和接码前检查 Plus；手动单个/批量补跑允许用户确认仅覆盖实时查询失败，明确查到非 Plus 时仍停止。

**Architecture:** 套餐查询仍由 `codex_retry_service.run_worker()` 统一调用。新增的 `plus_confirmed` 关键字参数默认是 `False`，因此现有调用保持严格模式；单个和批量手动补跑从确认框传入 `plus_confirmed=true`，仅当查询请求失败时放行，查询成功返回 free/Pro/Team/Go 等结果时忽略人工确认并停止。注册任务智能重试不传此参数，继续保持严格模式。

**Tech Stack:** Python 3.13、Flask、`unittest`/`pytest`、现有 JSON 账号数据库与 `core.chatgpt_plan`。

---

## File Map

- Modify: `core/plan_check_service.py` — 提供共享限速、落库和异常归一化的同步套餐查询函数。
- Modify: `core/codex_retry_service.py` — 实现实际 Plus 判定和统一补跑门禁。
- Modify: `webui/templates/index.html` — 更新补跑提示，明确先查 Plus、通过后才消耗 OTP/接码。
- Create: `tests/test_plan_check_sync.py` — 同步套餐查询的限速、落库和异常测试。
- Create: `tests/test_codex_retry_plus_gate.py` — Plus、非 Plus、试用资格和查询失败的补跑门禁测试。
- Create: `tests/test_webui_codex_retry_plus_gate_template.py` — 前端提示文本回归测试。

### Task 1: 同步套餐查询入口

**Files:**
- Modify: `core/plan_check_service.py`
- Create: `tests/test_plan_check_sync.py`

- [ ] **Step 1: 写成功查询的失败测试**

```python
import unittest
from unittest.mock import patch

from core import plan_check_service


class PlanCheckSyncTests(unittest.TestCase):
    def test_check_account_plan_now_uses_rate_limit_and_persists_result(self):
        result = {"ok": True, "current_plan_type": "plus", "checked_at": "2026-08-03T12:00:00"}
        with patch.object(plan_check_service, "_wait_for_rate_slot") as wait, \
             patch.object(plan_check_service, "check_account_plan", return_value=result) as check, \
             patch.object(plan_check_service.db, "update_account_plan_check") as update:
            actual = plan_check_service.check_account_plan_now(
                account_id=42,
                email="plus@example.com",
                access_token="token-value",
                trigger="codex_retry_gate",
            )

        self.assertEqual(actual, result)
        wait.assert_called_once_with()
        check.assert_called_once_with("token-value", proxy=None, timezone_offset_min="-")
        update.assert_called_once_with(acc_id=42, result=result)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_plan_check_sync.py -q`

Expected: FAIL，提示 `plan_check_service` 尚无 `check_account_plan_now`。

- [ ] **Step 3: 补充异常归一化测试**

```python
    def test_check_account_plan_now_returns_and_persists_structured_failure(self):
        with patch.object(plan_check_service, "_wait_for_rate_slot"), \
             patch.object(plan_check_service, "check_account_plan", side_effect=RuntimeError("network down")), \
             patch.object(plan_check_service.db, "update_account_plan_check") as update:
            actual = plan_check_service.check_account_plan_now(
                account_id=42,
                email="plus@example.com",
                access_token="token-value",
            )

        self.assertFalse(actual["ok"])
        self.assertIn("RuntimeError: network down", actual["error"])
        update.assert_called_once_with(acc_id=42, result=actual)
```

- [ ] **Step 4: 实现最小同步入口**

在 `core/plan_check_service.py` 增加：

```python
def check_account_plan_now(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "codex_retry_gate",
    proxy: str | None = None,
    timezone_offset_min: str = "-",
) -> dict:
    try:
        _wait_for_rate_slot()
        result = check_account_plan(
            access_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        logger.exception("[Plan] 同步查询异常: %s trigger=%s", email, trigger)

    db.update_account_plan_check(acc_id=int(account_id), result=result)
    return result
```

- [ ] **Step 5: 运行聚焦测试**

Run: `python -m pytest tests/test_plan_check_sync.py tests/test_plan_check_same_proxy.py -q`

Expected: 全部 PASS。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add -- core/plan_check_service.py tests/test_plan_check_sync.py
git commit -m "feat: add synchronous plan check"
```

### Task 2: Codex 补跑统一 Plus 门禁

**Files:**
- Modify: `core/codex_retry_service.py`
- Create: `tests/test_codex_retry_plus_gate.py`

- [ ] **Step 1: 写实际 Plus 判定的失败测试**

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_retry_service


class CodexRetryPlusGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir_handle = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_handle.name)

    def tearDown(self):
        self.temp_dir_handle.cleanup()

    def test_active_plus_plan_variants_pass(self):
        self.assertTrue(codex_retry_service._is_actual_plus({"current_plan_type": "plus"}))
        self.assertTrue(codex_retry_service._is_actual_plus({"current_plan_type": "chatgpt_plus"}))

    def test_trial_eligible_free_and_other_paid_plans_do_not_pass(self):
        self.assertFalse(codex_retry_service._is_actual_plus({
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }))
        for plan in ("pro", "team", "go", "unknown", ""):
            self.assertFalse(codex_retry_service._is_actual_plus({"current_plan_type": plan}))
```

- [ ] **Step 2: 运行判定测试并确认失败**

Run: `python -m pytest tests/test_codex_retry_plus_gate.py -q`

Expected: FAIL，提示 `_is_actual_plus` 尚不存在。

- [ ] **Step 3: 实现实际 Plus 判定与门禁函数**

在 `core/codex_retry_service.py` 增加：

```python
def _is_actual_plus(plan_result: dict) -> bool:
    plan = str(
        plan_result.get("current_plan_type")
        or plan_result.get("plan_type")
        or ""
    ).strip().lower()
    return "plus" in plan and "free" not in plan


def _check_plus_gate(email: str) -> dict:
    from core import plan_check_service

    account = db.get_account_by_email(email)
    if not account:
        return {"ok": False, "status": "failed", "message": "Plus 前置检验失败：账号不存在"}
    token = str(account.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "status": "failed", "message": "Plus 前置检验失败：账号缺少 access_token"}

    result = plan_check_service.check_account_plan_now(
        account_id=int(account.get("id") or 0),
        email=email,
        access_token=token,
        trigger="codex_retry_gate",
    )
    if not result.get("ok"):
        reason = str(result.get("error") or "套餐查询失败")
        return {"ok": False, "status": "failed", "message": f"Plus 前置检验失败：{reason}"}
    if not _is_actual_plus(result):
        return {
            "ok": False,
            "status": "skipped",
            "message": "当前未开通 Plus，未执行邮箱 OTP 和接码",
        }
    return {"ok": True, "plan_type": result.get("current_plan_type") or result.get("plan_type")}
```

- [ ] **Step 4: 写 worker 不消耗 OAuth 的失败测试**

使用临时日志路径并模拟账号、套餐查询与 OAuth：

```python
    def test_non_plus_worker_stops_before_oauth_and_sms(self):
        account = {"id": 7, "email": "free@example.com", "access_token": "token"}
        plan = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": True}
        with patch.object(codex_retry_service.db, "get_account_by_email", return_value=account), \
             patch("core.plan_check_service.check_account_plan_now", return_value=plan), \
             patch("core.codex_oauth.run_codex_oauth") as oauth, \
             patch.object(codex_retry_service.db, "update_account_codex_status") as update:
            result = codex_retry_service.run_worker(
                "free@example.com",
                target_log_path=self.temp_dir / "retry.log",
            )

        self.assertEqual(result["status"], "skipped")
        oauth.assert_not_called()
        update.assert_any_call(
            "free@example.com",
            "skipped",
            "当前未开通 Plus，未执行邮箱 OTP 和接码",
        )
```

另外添加：
- `plus` 和 `chatgpt_plus` 调用一次 OAuth；
- 套餐查询失败不调用 OAuth，返回 `failed`；
- Token 缺失不调用套餐查询和 OAuth；
- 每种提前结束场景均释放 `reserve()` 占位。

- [ ] **Step 5: 在 `run_worker()` 中接入门禁**

在账号日志处理器建立、配置热加载完成、调用 `run_codex_oauth()` 之前加入：

```python
        logger.info("[Codex 补跑] Plus 前置检验：开始实时查询当前套餐")
        gate = _check_plus_gate(email)
        if not gate.get("ok"):
            result = {
                "status": gate.get("status") or "failed",
                "ok": False,
                "message": gate.get("message") or "Plus 前置检验失败",
            }
            db.update_account_codex_status(email, result["status"], result["message"])
            logger.warning("[Codex 补跑] %s", result["message"])
            return result
        logger.info("[Codex 补跑] Plus 前置检验通过：plan=%s", gate.get("plan_type") or "plus")
```

确保 `run_codex_oauth()` 只出现在该判断之后，现有异常、停止和 `finally: release(email)` 逻辑不变。

- [ ] **Step 6: 运行聚焦测试**

Run: `python -m pytest tests/test_codex_retry_plus_gate.py tests/test_plan_check_sync.py -q`

Expected: 全部 PASS，且非 Plus/查询失败测试证明 OAuth 未被调用。

- [ ] **Step 7: 提交 Task 2**

```powershell
git add -- core/codex_retry_service.py tests/test_codex_retry_plus_gate.py
git commit -m "feat: require plus before codex retry"
```

### Task 3: WebUI 提示与完整验证

**Files:**
- Modify: `webui/templates/index.html`
- Create: `tests/test_webui_codex_retry_plus_gate_template.py`

- [ ] **Step 1: 写提示文本失败测试**

```python
from pathlib import Path


def test_codex_retry_confirmation_explains_plus_gate():
    html = Path("webui/templates/index.html").read_text(encoding="utf-8")
    assert "先实时确认当前已开通 Plus" in html
    assert "确认 Plus 后才会消耗邮箱 OTP 和接码短信" in html
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_webui_codex_retry_plus_gate_template.py -q`

Expected: FAIL，提示新文案不存在。

- [ ] **Step 3: 更新单个和批量补跑提示**

单个补跑确认框说明：
- 系统先实时确认当前已开通 Plus；
- 确认 Plus 后才消耗邮箱 OTP 和接码短信；
- 非 Plus 或套餐查询失败直接停止。

批量提示说明每个账号分别实时查询，只有实际 Plus 继续执行。

- [ ] **Step 4: 运行前端相关测试和 JavaScript 语法检查**

Run: `python -m pytest tests/test_webui_codex_retry_plus_gate_template.py tests/test_webui_extract_link_provider_template.py -q`

Expected: 全部 PASS。

将模板内联 `<script>` 内容提取到临时 `.js` 文件后运行：`node --check <temp-file>`。

Expected: exit code 0。

- [ ] **Step 5: 运行全量验证**

```powershell
python -m pytest -q
git diff --check
git status --short --branch
```

Expected: 所有测试通过；工作区除原有 `logs/` 外无未提交文件。

- [ ] **Step 6: 提交 Task 3**

```powershell
git add -- webui/templates/index.html tests/test_webui_codex_retry_plus_gate_template.py
git commit -m "docs: explain codex retry plus gate"
```

### Task 4: 人工确认仅覆盖套餐查询失败

**Files:**
- Modify: `core/codex_retry_service.py`
- Modify: `webui/app.py`
- Modify: `webui/templates/index.html`
- Modify: `tests/test_codex_retry_plus_gate.py`
- Create: `tests/test_webui_codex_retry_plus_confirmation.py`
- Modify: `tests/test_webui_codex_retry_plus_gate_template.py`

- [ ] **Step 1: 写服务层失败测试**

扩展测试 helper，使其接收 `plus_confirmed` 并原样传入 `run_worker()`，然后加入三条行为测试：

```python
def test_confirmed_plus_continues_when_plan_query_fails(self):
    result, _, oauth, update = self._run_worker(
        "confirmed@example.com",
        {"id": 10, "email": "confirmed@example.com", "access_token": "token"},
        {"ok": False, "error": "HTTP 401"},
        plus_confirmed=True,
    )
    self.assertTrue(result["ok"])
    oauth.assert_called_once_with("confirmed@example.com", force=True)
    update.assert_called_once_with("confirmed@example.com", "success", None)

def test_unconfirmed_plan_query_failure_still_stops(self):
    result, _, oauth, update = self._run_worker(
        "strict@example.com",
        {"id": 12, "email": "strict@example.com", "access_token": "token"},
        {"ok": False, "error": "HTTP 401"},
        plus_confirmed=False,
    )
    self.assertEqual(result["status"], "failed")
    oauth.assert_not_called()
    update.assert_called_once_with(
        "strict@example.com",
        "failed",
        "Plus 前置检验失败：HTTP 401",
    )

def test_confirmed_plus_does_not_override_successful_free_result(self):
    result, _, oauth, update = self._run_worker(
        "free-confirmed@example.com",
        {"id": 11, "email": "free-confirmed@example.com", "access_token": "token"},
        {"ok": True, "current_plan_type": "free"},
        plus_confirmed=True,
    )
    self.assertEqual(result["status"], "skipped")
    oauth.assert_not_called()
```

- [ ] **Step 2: 运行服务层测试并确认失败**

Run: `python -m pytest tests/test_codex_retry_plus_gate.py -q`

Expected: FAIL，提示 helper/`run_worker()` 尚不接收 `plus_confirmed`，或查询失败仍被拦截。

- [ ] **Step 3: 实现服务层最小改动**

```python
def _check_plus_gate(email: str, *, plus_confirmed: bool = False) -> dict:
    from core import plan_check_service

    account = db.get_account_by_email(email)
    if not account:
        return {"ok": False, "status": "failed", "message": "Plus 前置检验失败：账号不存在"}
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        return {
            "ok": False,
            "status": "failed",
            "message": "Plus 前置检验失败：账号缺少 access_token",
        }
    plan_result = plan_check_service.check_account_plan_now(
        account_id=int(account.get("id") or 0),
        email=email,
        access_token=access_token,
        trigger="codex_retry_gate",
    )
    if not plan_result.get("ok"):
        reason = str(plan_result.get("error") or "套餐查询失败")
        if plus_confirmed is True:
            return {
                "ok": True,
                "plan_type": "user_confirmed_plus",
                "warning": f"套餐查询失败，按用户确认继续：{reason}",
            }
        return {"ok": False, "status": "failed", "message": f"Plus 前置检验失败：{reason}"}
    if not _is_actual_plus(plan_result):
        return {
            "ok": False,
            "status": "skipped",
            "message": "当前未开通 Plus，未执行邮箱 OTP 和接码",
        }
    return {
        "ok": True,
        "plan_type": plan_result.get("current_plan_type") or plan_result.get("plan_type"),
    }

def run_worker(
    email: str,
    *,
    batch_label: str | None = None,
    clear_log: bool = True,
    target_log_path: str | Path | None = None,
    plus_confirmed: bool = False,
) -> dict:
    gate = _check_plus_gate(email, plus_confirmed=plus_confirmed is True)
    if gate.get("warning"):
        logger.warning("[Codex 补跑] %s", gate["warning"])
```

只有 `plan_result.ok == False` 的分支读取 `plus_confirmed`；成功返回的非 Plus 分支保持原样。

- [ ] **Step 4: 运行服务层测试并确认通过**

Run: `python -m pytest tests/test_codex_retry_plus_gate.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 写 Web API 与模板失败测试**

新增 Flask API 测试，替换 `threading.Thread` 为同步捕获对象，验证：

```python
client.post("/api/codex/retry", json={"email": "plus@example.com", "plus_confirmed": True})
# thread kwargs 包含 plus_confirmed=True

client.post("/api/codex/retry-bulk", json={
    "account_ids": [1],
    "workers": 1,
    "plus_confirmed": True,
})
# bulk runner 最终调用 worker 时包含 plus_confirmed=True
```

模板测试分别限定在单个/批量函数代码段内，断言确认文案包含“我确认账号当前已实际开通 Plus”，且请求体包含 `plus_confirmed: true`。

- [ ] **Step 6: 运行 Web 测试并确认失败**

Run: `python -m pytest tests/test_webui_codex_retry_plus_confirmation.py tests/test_webui_codex_retry_plus_gate_template.py -q`

Expected: FAIL，提示请求参数或模板字段尚不存在。

- [ ] **Step 7: 实现 Web 参数传递与简洁确认文案**

`webui/app.py`：

```python
plus_confirmed = data.get("plus_confirmed") is True
```

单个线程、批量 dispatcher、executor 和 `_run_codex_retry_worker()` 全链路显式传递该布尔值。非布尔真值（例如 `1`、`"true"`）按 `False` 处理。

`webui/templates/index.html`：复用现有单个与批量确认框，不增加额外按钮；确认框明确说明用户点击确定即表示“我确认账号当前已实际开通 Plus”，请求 JSON 增加 `plus_confirmed: true`。

- [ ] **Step 8: 运行聚焦验证并提交**

Run:

```powershell
python -m pytest tests/test_codex_retry_plus_gate.py tests/test_webui_codex_retry_plus_confirmation.py tests/test_webui_codex_retry_plus_gate_template.py -q
git diff --check
```

Expected: 全部 PASS；`git diff --check` 无错误。

Commit:

```powershell
git add -- core/codex_retry_service.py webui/app.py webui/templates/index.html tests/test_codex_retry_plus_gate.py tests/test_webui_codex_retry_plus_confirmation.py tests/test_webui_codex_retry_plus_gate_template.py docs/superpowers/plans/2026-08-03-codex-retry-plus-gate.md
git commit -m "fix: allow confirmed plus retry on plan query failure"
```

### Task 5: 运行验收

**Files:**
- No tracked file changes expected.

- [ ] **Step 1: 重启当前 WebUI 服务**

停止当前临时 `5003` WebUI，再从本工作树运行：

```powershell
python -u web.py --port 5003
```

保留 `5002` 的 Windows 残留监听现状，不切换或合并 `main`。

- [ ] **Step 2: 页面验收**

- 打开账号页，确认单个和批量补跑提示包含 Plus 前置检验说明。
- 通过自动化测试模拟非 Plus，确认日志没有进入邮箱 OTP、取号、发短信步骤。
- 对实际 Plus 测试账号只验证门禁通过测试；自动验收不触发真实 OTP 或真实接码消费。

- [ ] **Step 3: 最终验证**

Run: `python -m pytest -q`

Expected: 全量 PASS。

Run: `git diff --check; git status --short --branch`

Expected: 仅 `?? logs/`。
