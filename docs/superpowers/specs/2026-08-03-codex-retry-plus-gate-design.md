# Codex 补跑 Plus 前置检验设计

## 目标

所有“补跑 Codex”入口在消耗邮箱 OTP 或接码号码前，实时确认账号当前已实际开通 Plus。只有实时套餐结果为 Plus 的账号继续执行完整 Codex OAuth；`free（可 Plus 试用）`、其他套餐和查询失败均停止。

## 范围

- 覆盖账号页单个补跑、账号页批量补跑、注册任务智能重试触发的 Codex 补跑。
- 不改变正常注册完成后自动执行 Codex OAuth 的流程。
- 不新增前端开关或操作步骤，沿用现有补跑按钮、状态、错误信息和日志查看入口。
- “实际 Plus”沿用账号列表已有判定：实时 `current_plan_type`/`plan_type` 包含 `plus` 且不包含 `free`。`plus_trial_eligible=true` 本身不通过。

## 推荐方案

在 `core/codex_retry_service.py` 的统一 `run_worker()` 中设置前置检验，而不是只在 Web API 入口判断。这样单个、批量和任务重试不会出现入口遗漏，也避免从点击到真正执行之间套餐状态变化。

套餐查询复用 `core.chatgpt_plan.check_account_plan()` 和现有套餐查询网络配置、超时及重试规则。查询请求进入现有套餐查询限速器，结果继续写回账号套餐字段，保证账号页展示与本次门禁结论一致。

## 数据流

1. 补跑入口完成账号存在性、废号状态和重复运行检查，按现有逻辑占用任务。
2. `run_worker()` 建立账号日志并热加载配置。
3. 从数据库重新读取账号，取得账号 ID、邮箱和 `access_token`。
4. 通过套餐查询服务执行一次实时查询，并将结果写回数据库。
5. 根据本次查询结果判断：
   - 实际 Plus：记录通过日志，然后调用 `run_codex_oauth()`。
   - 查询成功但不是实际 Plus：返回 `skipped`，写入“当前未开通 Plus，未执行邮箱 OTP 和接码”。
   - Token 缺失、Token 过期、网络错误、接口错误或响应不可解析：返回 `failed`，保留明确查询错误，未执行邮箱 OTP 和接码。
6. 无论门禁结果如何，继续使用现有 `finally` 释放补跑占位。

## 组件边界

### 套餐查询服务

增加一个供补跑门禁调用的同步查询函数，负责：

- 使用统一限速器；
- 调用 `check_account_plan()`；
- 更新账号套餐查询结果；
- 捕获异常并返回结构化失败结果。

该函数不占用异步套餐查询队列，不等待另一个后台任务完成，但和现有查询共享请求启动节奏。

### Codex 补跑服务

增加小型 Plus 判定与门禁函数。异常捕获仅覆盖门禁查询；门禁通过后，现有 OAuth、邮箱 OTP、接码、停止信号和结果保存逻辑保持不变。

## 状态与提示

- 非 Plus：`codex_status=skipped`，`codex_error=当前未开通 Plus，未执行邮箱 OTP 和接码`。
- 查询失败：`codex_status=failed`，错误信息以 `Plus 前置检验失败：` 开头并附套餐查询原因。
- Plus 通过：继续使用现有 `retrying -> success/failed/deactivated/stopped` 状态流转。
- 批量补跑中，不符合条件的账号各自结束，不影响同批其他 Plus 账号。

## 测试

- 实时查询返回 `plus`：调用一次 `run_codex_oauth()`。
- 返回 `chatgpt_plus`：允许继续。
- 返回 `free` 且 `plus_trial_eligible=true`：不调用 OAuth，状态为 `skipped`。
- 返回 Pro、Team、Go 或未知套餐：不调用 OAuth，状态为 `skipped`。
- 查询失败、Token 缺失或 Token 过期：不调用 OAuth，状态为 `failed`。
- 套餐查询结果写回数据库。
- 单个、批量、任务重试最终都经过同一个 `run_worker()` 门禁。
- 门禁结束后补跑占位正常释放，现有停止功能与全量测试无回归。

## 非目标

- 不把 Plus 试用资格当作已开通 Plus。
- 不自动购买、开通或提取 Plus 链接。
- 不在前端增加第二个确认框、额外按钮或复杂状态机。
