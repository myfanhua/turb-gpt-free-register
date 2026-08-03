# 注册页邮箱来源选择设计

**日期：** 2026-08-02
**分支：** `codex/hero-sms-provider`
**目标：** 在注册页为每一批注册任务选择邮箱来源，并保证不同批次、不同并发任务之间不会因全局配置变化而串用邮箱来源。

## 1. 用户界面

在“启动批量注册”区域增加“邮箱来源”下拉框，与注册数量、并发线程数并列。

选项如下：

```text
跟随当前配置
iCloud 全部
iCloud API
iCloud 独立 URL
Outlook
通用 API
Cloudflare 域名邮箱
Cloudflare 临时邮箱
GPTMail
MailNest
CloudMail
```

其中：

- **跟随当前配置**：继续使用 `EMAIL_SOURCE`，包括现有的多来源顺序兜底能力。
- **iCloud 全部**：可领取任何可用 iCloud 记录，并使用该记录原本的取件模式。
- **iCloud API**：只领取含 Token 的 iCloud 记录；即使记录同时含 URL，本次任务也强制只走 Token API。
- **iCloud 独立 URL**：只领取含独立 URL 的 iCloud 记录；即使记录同时含 Token，本次任务也强制只走 URL，不回退 API。
- 其他选项：只使用该单一邮箱来源，不读取全局来源顺序。

不增加单独的“URL + API 后备”前端选项。现有同时含 Token 和 URL 的记录，在“iCloud 全部”中保持记录原本的 URL → API 后备行为；在“iCloud API”或“iCloud 独立 URL”中按本批选择强制单通道执行。

## 2. 来源标识

继续保留现有真实来源标识，并增加两个仅用于注册批次选择的虚拟来源：

```text
icloud_api        # iCloud 全部，兼容现有配置
icloud_api_token  # iCloud API 强制模式
icloud_url        # iCloud 独立 URL 强制模式
```

虚拟来源只影响邮箱领取和本次取件策略。注册成功账号的实际邮箱来源仍记录为 `icloud_api`，保证现有验证码读取、释放邮箱、账号筛选和导出逻辑兼容。

## 3. 每批任务隔离

前端启动注册时提交：

```json
{
  "count": 3,
  "workers": 3,
  "email_source": "icloud_url"
}
```

后端把选择写入每个任务的 `email_source` 字段。工作线程启动后，从自己的任务记录读取来源，再领取邮箱。

禁止通过修改全局 `EMAIL_SOURCE` 实现本批选择，原因是多个批次可能同时运行：

```text
批次 A：icloud_url
批次 B：outlook
```

两批任务必须分别使用自己的来源，即使它们共用同一个线程池、同时排队或先后切换并发数。

未提交 `email_source` 或提交空字符串时，任务在创建时把当时的 `EMAIL_SOURCE` 快照写入任务，后续配置变化不影响已经创建的任务。

## 4. iCloud 领取与强制取件模式

`claim_next_icloud_email()` 增加领取过滤参数：

```text
all    # 任意可用记录
token  # token 非空
url    # pickup_url/pickupUrl 非空
```

领取记录时同时保存本次运行策略 `claimed_pickup_mode`：

- `all`：使用记录自身推导的 `pickup_mode`。
- `token`：保存 `api_token`。
- `url`：保存 `independent_url`。

该字段随 `used` 状态持久化，以便任务上下文缓存丢失或服务重载后仍能恢复本批选择。邮箱重新释放为 `available` 时清除 `claimed_pickup_mode`。

构造 `ICloudMailAccount` 时：

- API 强制模式保留 Token，并忽略浏览器独立页面 URL；若记录带明确 JSON Pickup endpoint，可继续使用该 endpoint。
- URL 强制模式保留独立 URL，并在运行上下文中清空 Token，确保不会进入 API/Profile 后备。
- 全部模式沿用记录当前的 `pickup_mode`。

同一条同时含 Token 和 URL 的邮箱可被 API 批次或 URL 批次领取，但邮箱池的原子 `available → used` 状态保证它不会被两个并发任务重复领取。

## 5. 后端 API 与校验

`POST /api/jobs` 接受可选 `email_source`。

- 空值：使用当前配置快照。
- 单一有效来源：使用所选来源。
- 未知来源或多个来源字符串：返回 HTTP 400，不创建任务。

配置前置校验和邮箱池容量提示必须基于本批来源，而不是全局 `EMAIL_SOURCE`：

- `gptmail` 检查 GPTMail API Key。
- `mailnest` 检查 API Key 和项目代码。
- `cloudmail` 检查 API 地址和 Token。
- `cloudflare` 检查 API 地址和所选鉴权配置。
- `icloud_api` 统计所有可用 iCloud 记录。
- `icloud_api_token` 只统计含 Token 的可用记录。
- `icloud_url` 只统计含独立 URL 的可用记录。
- Outlook、通用 API 和域名邮箱沿用现有容量提示。

增加只读的来源元数据接口或在页面初始化数据中返回：

```json
{
  "configured": "icloud_api",
  "options": [
    {"value": "icloud_api", "label": "iCloud 全部"},
    {"value": "icloud_api_token", "label": "iCloud API"},
    {"value": "icloud_url", "label": "iCloud 独立 URL"}
  ]
}
```

接口不返回任何 Token、URL、API Key 或其他秘密。

## 6. 重试与任务展示

- 任务列表继续显示任务创建时保存的 `email_source`。
- 注册任务重试时继承原任务的来源选择。
- Codex 补跑保持现有逻辑，不重新领取邮箱。
- 前端提交成功提示包含本批选择的来源标签。
- 刷新页面后下拉框默认回到“跟随当前配置”，不自动复用上一次临时选择。

## 7. 兼容性

- 现有未传 `email_source` 的 API 调用保持有效。
- 现有 `EMAIL_SOURCE=icloud_api` 的行为不变，仍表示 iCloud 全部。
- 现有多来源配置继续只在“跟随当前配置”时生效。
- Outlook、GPTMail、MailNest、CloudMail、Cloudflare 等现有客户端接口保持不变，仅让 `acquire_email()` 接受可选来源参数。
- 旧 iCloud JSON 记录不要求迁移；`pickup_url` 与 `pickupUrl` 均可参与 URL 过滤。

## 8. 测试要求

### 前端与 API

- 注册页包含完整来源下拉选项。
- 选择 `icloud_url` 时 POST body 包含该值。
- 未选择时提交空值并使用配置快照。
- 非法来源返回 400 且不创建任务。
- 配置检查和容量提示使用所选来源。

### 调度隔离

- `acquire_email("outlook")` 只调用 Outlook。
- `acquire_email("icloud_api_token")` 只领取含 Token 的 iCloud。
- `acquire_email("icloud_url")` 只领取含 URL 的 iCloud。
- 两个任务分别保存 `icloud_url` 和 `outlook` 时，各自调用对应来源。
- 创建任务后修改全局 `EMAIL_SOURCE` 不改变任务实际来源。
- 注册重试继承原任务来源。

### iCloud 通道

- API 强制模式对 Token + URL 记录只发 JSON API 请求。
- URL 强制模式对 Token + URL 记录只发 HTML URL 请求，不调用 Pickup/Profile。
- iCloud 全部继续保持原记录的取件策略。
- API、URL 两个并发领取不会获得同一邮箱。
- 邮箱释放后清除临时领取策略。

### 回归

- 现有 iCloud、邮箱池、WebUI、注册服务、HeroSMS 测试通过。
- 完整测试套件通过。
- `logs/`、`.env`、原始 Token 和独立 URL 不进入提交或 API 响应。

## 9. 完成标准

1. 用户可以在注册页为每一批任务选择单一邮箱来源。
2. “iCloud API”和“iCloud 独立 URL”分别强制使用所选通道。
3. 不同批次并发运行不会互相覆盖或串用来源。
4. 不选择时完整保留当前配置行为。
5. 重试、容量提示、配置检查和任务展示均与本批来源一致。
6. 针对性测试、完整回归和凭据扫描全部通过。
