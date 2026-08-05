# 缺失 Access Token 自动补全设计

## 目标

为已存在于账号列表、但 `access_token` 为空的账号增加独立的自动补 AT 功能。用户可以从账号行发起单账号补全，也可以勾选多个账号批量补全。

补全流程只登录 ChatGPT 网页版，通过账号原邮箱来源自动读取一次性验证码。流程不重新注册账号，不启动 Codex OAuth，不调用接码平台，也不进入手机验证流程。成功后完整更新账号资料并刷新现有导出文件和账号页面。

## 范围

### 包含

- 单账号自动补 AT。
- 勾选账号的批量自动补 AT。
- 已有 AT 的账号自动跳过。
- 复用账号保存的代理和设备身份。
- 缺失代理或设备身份时自动兜底。
- 自动读取邮箱验证码并完成网页版登录。
- 获取 `/api/auth/session` 中的 Access Token 和账号元数据。
- 后台任务状态、日志、停止和进程重启后的中断恢复。
- 账号页实时展示补 AT 状态和错误。

### 不包含

- 刷新或替换已有 Access Token。
- 使用邮箱 `refresh_token` 刷新 ChatGPT Access Token。
- 重新注册账号或修改账号资料。
- Codex OAuth、套餐查询或 Codex Agent Token 生成。
- 手机验证码、HeroSMS、SMS-Activate 或其他接码通道。
- 手工粘贴 Access Token。

## 架构

### `core/access_token_recovery_service.py`

负责补 AT 任务的编排：

- 校验并重新读取账号。
- 原子领取账号的补 AT 状态，避免同一账号重复执行。
- 管理单个和批量入队。
- 使用独立后台执行器运行任务，批量中单个账号失败不影响其他账号。
- 提供排队、运行、停止、成功和失败状态。
- 为每个账号写入独立任务日志。
- 进程启动时把遗留的 `queued` / `running` 状态恢复为中断失败，避免永久忙碌。

### `core/roxy_access_token_recovery.py`

负责单账号网页版登录：

- 打开 Roxy 登录环境。
- 注入账号设备身份。
- 输入已有账号邮箱。
- 在密码页优先选择“使用一次性验证码登录”。
- 使用现有邮箱来源调度层获取新验证码。
- 提交验证码并识别登录结果。
- 获取 `/api/auth/session`。
- 返回 Access Token、用户、套餐、过期时间和其他可保存元数据。
- 识别手机号验证、账号停用、验证码失败、登录超时和 session 缺少 Token 等终止状态。

该模块可以复用 `core/roxy_registration.py` 中已经验证过的浏览器启动、邮箱输入、OTP、页面状态和 session 读取能力，但不得调用注册入口 `run_roxy_registration()`，避免进入创建账号、资料页、Codex 和邮箱回收逻辑。可复用逻辑如需调整，应提取为边界清晰的内部帮助函数，注册流程与补 AT 流程分别负责自己的业务编排。

### 数据库层

账号记录增加以下字段：

- `at_recovery_status`：`queued`、`running`、`success`、`failed`、`stopped`。
- `at_recovery_error`：最近一次失败或停止原因。
- `at_recovery_trigger`：`manual` 或 `manual_bulk`。
- `at_recovery_started_at`。
- `at_recovery_completed_at`。
- `at_recovery_log_file`：任务日志位置。
- `at_recovery_stop_requested`：运行中任务的协作式停止标记；不作为用户可操作状态展示。

数据库提供原子领取、标记运行、完成、失败、停止和中断恢复方法。成功写回账号时必须在同一锁保护范围内更新账号记录并触发现有账号 JSON、Token 文本和静态查看页同步。

## 数据流

### 提交阶段

1. WebUI 提交一个或多个账号 ID。
2. 服务逐个重新读取账号，不能依赖页面缓存数据。
3. 账号不存在时加入 `skipped`。
4. `access_token` 已存在时加入 `skipped`，不覆盖现有 Token。
5. 状态为 `queued` 或 `running` 时加入 `busy`。
6. 其余账号通过数据库原子领取后加入执行器。

### 登录环境

1. 优先使用账号的 `proxy_used`。
2. `proxy_used` 为空时调用当前注册代理选择器取得本次代理。
3. 优先使用账号的 `device_id`。
4. `device_id` 为空时生成新的 UUID；只有成功取得有效 session 后才正式写回账号。
5. Roxy 创建环境必须支持显式传入上述代理；保存的是本次实际交给 Roxy 的代理地址。
6. 登录环境沿用现有 Roxy 启动门控、窗口清理和代理桥恢复能力。保存的是本地代理桥地址时，启动前先恢复对应 SID 的桥接路由。

### 网页登录

1. 打开 `https://chatgpt.com/auth/login`。
2. 输入账号邮箱并提交。
3. 已经存在有效登录态时直接读取 session。
4. 出现密码页时点击“使用一次性验证码登录”。不填写随机密码，也不把邮箱密码当作 ChatGPT 密码。
5. 记录发送验证码前的时间戳，通过 `core.email_provider.wait_for_otp()` 按账号原邮箱来源读取新验证码。
6. 验证码错误或过期时沿用现有重新发送逻辑，最多尝试三轮。
7. 验证通过后等待 ChatGPT session 写入。
8. 出现手机号验证页面时立即结束当前账号任务并记录明确原因，不调用任何接码模块。

### 成功写回

只有 `/api/auth/session` 返回非空 `accessToken` 后才更新账号。写回内容包括：

- `access_token`。
- `user_id`。
- `user_name`。
- `plan_type`。
- `expires_at`。
- `device_id`。
- `proxy_used`：本次成功登录实际使用的代理；账号原来已有值时保持同一路由，缺失时保存兜底代理。
- session 中已有的用户、账号和过期信息。
- `at_recovery_status=success` 及完成时间。

成功后同步刷新：

- `注册成功的邮箱.json`。
- `注册成功的邮箱.txt`。
- `注册成功的token.txt`。
- `accounts_viewer.html`。
- WebUI 账号列表数据。

完整 Access Token 不出现在普通日志、错误信息、批量响应或账号列表接口中。

## 停止与并发

- 单账号和批量任务均支持停止。
- 排队任务收到停止请求后不再启动浏览器，状态写为 `stopped`。
- 运行任务在浏览器启动、邮箱提交、验证码等待、验证码提交和 session 等待阶段检查停止标记。
- 运行任务停止时关闭本次浏览器环境并保留账号原数据。
- 同一账号同一时间只有一个补 AT 任务。
- 批量任务沿用项目当前并发配置；单个账号失败不取消同批其他账号。
- 补 AT 状态不与注册任务、Codex 补跑、套餐查询、提链或 Codex Agent 状态混用。

## WebUI

### 账号行

- `access_token` 为空且未运行时显示“补 AT”。
- 状态为 `queued` / `running` 时显示“补 AT 中”和“停止”。
- 状态为 `failed` / `stopped` 时显示简短原因，并允许再次补 AT。
- 有补 AT 记录时可查看最近一次任务日志。
- 成功后沿用现有 Token 复制和账号操作。

### 批量操作

- 新增“补缺失 AT”。
- 新增“停止补 AT”，只作用于选中账号中排队或运行的任务。
- 只处理当前勾选账号。
- 提交确认框显示可执行数量和将跳过的已有 AT 数量。
- 返回并展示 `started`、`busy`、`failed` 和 `skipped` 分类统计。

### API

- `POST /api/accounts/recover-access-token`
  - Body：`{account_id|id}`。
- `POST /api/accounts/recover-access-token-bulk`
  - Body：`{account_ids:[...]}`。
- `POST /api/accounts/recover-access-token/<account_id>/stop`
  - 停止排队或运行中的任务。
- `POST /api/accounts/recover-access-token/stop-bulk`
  - Body：`{account_ids:[...]}`，停止选中的补 AT 任务。
- `GET /api/accounts/recover-access-token/<account_id>/log`
  - 返回该账号最近一次补 AT 日志尾部，不返回完整 Access Token。

现有账号列表接口返回补 AT 状态字段和经过截断、清洗的错误信息，不返回新的完整 Token 字段。

## 错误处理

以下错误只影响当前账号：

- 邮箱不存在或邮箱来源无法解析。
- 邮箱取件凭据缺失或失效。
- 验证码等待超时。
- 验证码连续错误或过期。
- 登录页要求手机号验证。
- 账号已删除或停用。
- 代理或 Roxy 浏览器启动失败。
- 用户主动停止。
- session 请求失败。
- session 返回成功但缺少 `accessToken`。

失败时不得修改已有账号身份字段、邮箱池状态或 Token 导出文件。错误消息应保留足够诊断信息，同时清洗完整 Token、邮箱取件 Token、代理密码和认证 URL。

## 测试

### 数据库测试

- 仅缺少 AT 的账号可领取任务。
- 已有 AT 的账号被跳过。
- 同一账号不能重复领取。
- 状态流转和时间字段正确。
- 成功写回完整账号资料并同步导出文件。
- 失败或停止不修改账号原数据。
- 进程重启后恢复遗留任务状态。

### 服务测试

- 单账号和批量结果正确分类为 `started`、`busy`、`failed`、`skipped`。
- 批量中一个账号失败不影响其他账号。
- 账号代理和设备 ID 优先于默认值。
- 缺失代理时使用当前代理配置。
- 缺失设备 ID 时生成 UUID，并仅在成功后保存。
- 停止排队和运行任务。
- 日志和返回值不包含完整 Access Token。

### 浏览器流程测试

- 已有 session 时直接取得 Token。
- 邮箱提交后进入 OTP 页。
- 密码页正确切换到一次性验证码登录。
- 按账号原邮箱来源读取验证码。
- 验证码失败时重新发送并重试。
- 手机验证页终止流程且不调用接码模块。
- 账号停用、登录超时和 session 缺少 Token 返回明确错误。
- 浏览器关闭和 Roxy profile 清理符合现有配置。

### API 与 WebUI 测试

- 单个、批量和停止接口的状态码及响应结构。
- 账号行按钮随状态正确切换。
- 批量确认和结果统计正确。
- 账号列表不暴露完整 Token。
- 页面轮询后能够看到成功或失败状态。

### 回归与人工验收

- 运行新增测试及项目全量测试。
- 验证现有注册、Codex 补跑、套餐查询、提链和账号导出流程不受影响。
- 人工验收优先使用最新注册且 Access Token 已失效的账号；如本地存在多条缺失 AT 记录，再分别测试单账号补全、批量补全、停止和失败展示。自动化测试仍使用隔离 fixture，不依赖本地真实账号数据。

## 验收标准

- 用户可以对单个或多个缺失 AT 的账号发起自动补全。
- 任务只执行 ChatGPT 网页登录和邮箱 OTP，不调用手机号接码。
- 成功后账号拥有可用 Access Token，账号元数据和导出文件同步更新。
- 已有 Access Token 不被覆盖。
- 失败、停止和并发冲突均有明确状态与日志。
- 批量中单账号失败不影响其他账号。
- 页面和普通日志不泄露完整 Access Token 或邮箱、代理凭据。
- 全量测试通过。
