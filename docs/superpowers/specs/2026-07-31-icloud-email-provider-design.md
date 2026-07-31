# iCloud Pickup 邮箱来源设计

## 目标

将 Chrome 中验证过的 iCloud Mail Search 取件 API 作为新的邮箱来源 `icloud_api` 接入项目。系统支持在 WebUI 中批量导入多组“邮箱 + 邮箱专属 Token”，并保证并发注册任务原子领取、独占绑定和定向取码，避免邮箱或验证码串到其他任务。

## 范围

- 新增邮箱来源标识 `icloud_api`，可单独使用，也可加入 `EMAIL_SOURCE` 的有序兜底列表。
- 在 WebUI 邮箱池增加 iCloud 邮箱批量导入、列表、状态汇总和回收操作。
- 批量导入格式固定为每行 `邮箱----Token`，同时支持文本粘贴和 `.txt` 文件上传。
- 调用只读接口 `GET https://icloud.flysms.top/icloud/api/pickup/messages/latest` 获取指定邮箱的最新邮件。
- 每次请求显式发送 `X-Mailbox-Email` 与 `Authorization: Bearer <Token>`，不使用进程级“当前邮箱”变量。
- 将任务领取的邮箱写入注册任务记录；Token 仅保存在本地邮箱池数据中，日志和普通接口响应中不输出原值。
- WebUI 仅显示 Token 脱敏值，例如 `tok_****abcd`。
- 沿用项目现有 OTP 提取工具，从邮件主题、纯文本和 HTML 中提取六位验证码。

## 非目标

- 不通过浏览器自动操作 iCloud Mail Search 页面取件。
- 不使用页面生成的 URL Fragment 取件链接，因为后端 HTTP 请求不会发送 Fragment，且该页面依赖浏览器 JavaScript。
- 不自动创建、购买、续期或删除服务端邮箱。
- 不改变现有 Outlook、Generic API、Cloudflare、GPTMail、MailNest 和 CloudMail 来源的行为。
- 本轮不引入新的主密钥系统；Token 按项目现有本地凭据模型存储，并通过 `.gitignore`、接口过滤和 UI 脱敏防止意外暴露。

## 数据模型

新增独立的 iCloud 邮箱池持久化文件，由 `core/db.py` 在现有进程锁内原子读写。每条记录包含：

```text
id
email
token
status          available | used | registered | failed | disabled
used_at
created_at
updated_at
note
```

约束与导入规则：

- 邮箱以去除首尾空白后的全小写值作为唯一键。
- Token 必须非空；导入时去除首尾空白，但不改变 Token 内容。
- 同一批次中的重复邮箱以后出现的 Token 为准。
- 数据库中已有邮箱再次导入时更新 Token，并将 `disabled`、`failed` 或 `available` 记录恢复为 `available`；`used` 和 `registered` 状态保持不变，避免覆盖正在运行或已经消费的记录。
- 导入结果返回 `inserted`、`updated`、`skipped`、`invalid` 及逐行错误摘要。
- 所有返回给 WebUI 的记录在序列化前移除 `token`，另行生成 `token_masked`。

## 组件设计

### 配置

`config/email.py` 增加：

- `icloud_api` 来源说明与来源解析支持。
- `ICLOUD_PICKUP_API_BASE`，默认值为 `https://icloud.flysms.top/icloud/api/pickup`。
- `ICLOUD_PICKUP_TIMEOUT`，控制单次请求超时。

两个配置项加入 `.env` 热加载和 WebUI 配置白名单；Token 不进入全局配置，因为它属于单个邮箱记录。

### iCloud 客户端

新增 `core/icloud_mail_client.py`：

- `ICloudMailAccount` 保存任务领取到的 `email` 与 `token`。
- `pick_account()` 调用数据库原子领取接口，并返回唯一账号上下文。
- 上下文缓存以规范化邮箱为键，不使用共享的单例当前账号。
- `fetch_latest_otp(email, after_ts, ...)` 根据邮箱读取对应上下文，使用该上下文的 Token 发起请求。
- 每次响应必须同时满足：顶层 `email` 与请求邮箱一致、`message.to` 包含请求邮箱、邮件时间不早于 `after_ts` 的允许偏差。任一身份信号不匹配时不提取验证码，并记录不含 Token 的诊断信息。
- `release_account()` 按任务结果更新邮箱池状态并清理对应邮箱的上下文。

### 来源路由

`core/email_provider.py` 增加 `icloud_api` 分支：

- 领取：调用 iCloud 客户端的 `pick_account()`。
- 来源识别：根据 iCloud 邮箱池中是否存在该邮箱判断。
- 取码：把当前任务的显式邮箱传给 iCloud 客户端。
- 回收：仅更新该邮箱记录，不影响其他已领取邮箱。

来源解析顺序保持用户配置顺序，例如：

```dotenv
EMAIL_SOURCE=icloud_api,outlook
```

### WebUI 邮箱池

在现有邮箱池页面增加“iCloud API”分区：

- 文本域支持一次粘贴多行 `邮箱----Token`。
- 文件导入仅接受文本内容，沿用同一解析函数。
- 导入预览显示有效行、重复行和错误行数量。
- 列表展示邮箱、脱敏 Token、状态、领取时间和备注。
- 支持将未运行记录恢复为可用、停用和删除；运行中的 `used` 记录不可删除。
- API 响应、浏览器控制台和服务端日志均不包含 Token 原值。

## 并发隔离与数据流

1. WebUI 提交多个注册任务。
2. 每个工作线程调用 `acquire_email()`。
3. 数据库在同一个锁临界区内选择一条 `available` 记录并立即写为 `used`，因此两个线程不会领取同一邮箱。
4. 返回的 `ICloudMailAccount(email, token)` 以邮箱键写入上下文；任务记录只保存邮箱。
5. 等待验证码时，注册流程传入该任务自己的邮箱。
6. 客户端按邮箱取回对应 Token，构造专属请求头，并验证响应邮箱、收件人和邮件时间。
7. 成功注册后将邮箱标记为 `registered`；任务未消费失败时按现有规则原子恢复为 `available`；凭据错误或邮箱失效时标记为 `disabled`。

该流程不依赖线程名、全局当前邮箱或共享验证码缓存。任务与邮箱的关联由数据库领取结果和显式函数参数共同确定。

## API 契约与错误处理

请求：

```http
GET /messages/latest
X-Mailbox-Email: mailbox@icloud.com
Authorization: Bearer tok_xxx
Accept: application/json
```

成功响应只接受包含对象型 `message` 的 JSON。验证码候选来自 `subject`、`text`、`html`，并交给项目现有 OTP 工具处理。

状态处理：

- `200`：校验邮箱身份与时间后提取验证码；未出现新验证码则继续轮询。
- `401`：凭据无效或不匹配，停止轮询并将邮箱标记为 `disabled`。
- `403`：邮箱到期、停用或封禁，停止轮询并标记为 `disabled`。
- `404`：当前没有邮件，按正常空收件箱继续轮询。
- `429`：读取 `Retry-After`，在剩余总等待时间内按服务端建议退避。
- `503`：邮箱初始化中或临时不可刷新，继续轮询并保留最后错误摘要。
- 其他 `5xx` 与网络异常：有限重试；最终错误包含邮箱和状态，不包含 Token。
- 无效 JSON、响应邮箱不匹配、收件人不匹配或邮件时间无效：不接受验证码，最终以明确错误结束。

轮询使用单调时钟控制总超时。只接受任务触发验证码之后的新邮件，并沿用 `OTP_SETTLE_SECONDS` 稳定窗口，避免读取接口中残留的旧验证码。

## 验证策略

实现阶段先增加失败测试，再逐步实现：

- 批量导入解析：有效行、空行、错误分隔符、重复邮箱和更新 Token。
- Token 脱敏与 API 序列化：任何列表和导入响应均不出现原 Token。
- 并发领取：多个线程同时领取时，每个线程得到不同邮箱，池状态与领取数量一致。
- 请求隔离：不同邮箱产生不同的 `X-Mailbox-Email` 和 `Authorization` 请求头。
- 响应绑定：顶层邮箱或 `message.to` 不匹配时拒绝 OTP。
- 时间过滤：旧邮件验证码不被接受，新邮件验证码能够提取。
- 状态码：覆盖 `401`、`403`、`404`、`429`、`503` 和网络异常。
- 来源路由：`icloud_api` 能单独使用，也能与其他邮箱来源按顺序兜底。
- WebUI：批量导入、列表脱敏、恢复、停用和删除接口。
- 回归：运行项目完整测试，确认现有邮箱来源与任务流程不受影响。

## 完成标准

- WebUI 可一次导入多组 `邮箱----Token`，并正确报告新增、更新和错误数量。
- 并发任务不会领取同一 iCloud 邮箱。
- 每次取码请求只使用当前任务绑定的邮箱和 Token。
- 响应身份不一致或旧邮件不会产生验证码结果。
- Token 不出现在普通日志、任务记录、列表接口或页面明文中。
- 新增测试和现有测试全部通过。
