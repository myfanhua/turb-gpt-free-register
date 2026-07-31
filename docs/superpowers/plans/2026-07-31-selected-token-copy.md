# Selected Token Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让账号页顶部“复制Token”按钮在有勾选账号时只复制勾选账号的 Token，无勾选时继续复制当前页 Token。

**Architecture:** 保留现有 `/api/accounts/secret-bulk` 按需读取敏感值的接口，只调整前端账号 ID 的选择策略。通过模板回归测试固定“选择集优先、当前页回退、提示复制数量”的行为。

**Tech Stack:** Flask/Jinja HTML 模板、原生 JavaScript、Python unittest/pytest

---

### Task 1: 修复 Token 复制范围

**Files:**
- Create: `tests/test_webui_account_token_copy_template.py`
- Modify: `webui/templates/index.html:2228-2233`

- [ ] **Step 1: 写入失败的模板回归测试**

```python
# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class AccountTokenCopyTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("webui/templates/index.html").read_text(encoding="utf-8")

    def test_copy_tokens_prefers_selected_accounts(self):
        self.assertIn(
            "const selectedIds = Array.from(ACCOUNT_SELECTED).map(Number);",
            self.html,
        )
        self.assertIn("selectedIds.length ? selectedIds", self.html)

    def test_copy_tokens_falls_back_to_current_page_and_reports_count(self):
        self.assertIn(
            "ACCOUNTS.filter(r => r.has_access_token).map(r => Number(r.id))",
            self.html,
        )
        self.assertIn("已复制 ${tokens.length} 个 Token", self.html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认测试因行为尚未实现而失败**

Run:

```powershell
python -m pytest tests/test_webui_account_token_copy_template.py -q
```

Expected: 两个测试失败，分别缺少 `selectedIds` 选择逻辑和按数量提示。

- [ ] **Step 3: 最小修改复制按钮处理器**

把 `webui/templates/index.html` 中 `copyAllTokens` 点击处理器替换为：

```javascript
$('#copyAllTokens').addEventListener('click', async () => {
  const selectedIds = Array.from(ACCOUNT_SELECTED).map(Number);
  const ids = selectedIds.length ? selectedIds : ACCOUNTS.filter(r => r.has_access_token).map(r => Number(r.id));
  if (!ids.length) {
    showToast(selectedIds.length ? '选中账号没有 Token' : '当前页没有 Token');
    return;
  }
  try {
    const tokens = await fetchAccountSecrets(ids, 'access_token');
    if (!tokens.length) {
      showToast(selectedIds.length ? '选中账号没有 Token' : '当前页没有 Token');
      return;
    }
    await copyText(tokens.join('\n'));
    showToast(`已复制 ${tokens.length} 个 Token`);
  } catch(e) {
    showToast('复制失败: ' + e.message);
  }
});
```

- [ ] **Step 4: 运行新增测试并确认通过**

Run:

```powershell
python -m pytest tests/test_webui_account_token_copy_template.py -q
```

Expected: `2 passed`。

- [ ] **Step 5: 运行完整验证**

Run:

```powershell
$env:PROXY_CHAIN_ENABLED='false'
python -m pytest -q
git diff --check
```

Expected: 全部测试通过，`git diff --check` 退出码为 0。

- [ ] **Step 6: 重启并检查 WebUI**

重启当前 `web.py --host 127.0.0.1 --port 5000 --auth-code icloud-test` 进程，然后运行：

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:5000' -UseBasicParsing -TimeoutSec 5).StatusCode
```

Expected: `200`。

- [ ] **Step 7: 提交修复**

```powershell
git add webui/templates/index.html tests/test_webui_account_token_copy_template.py
git commit -m "fix: copy tokens from selected accounts"
```
