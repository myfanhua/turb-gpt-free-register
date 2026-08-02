# Kakao 批量提链 Provider 设计

## 背景

项目现有提链服务使用单账号任务协议：客户端调用 `POST /api/extract` 获取 `job_id`，再通过 `/api/jobs/{job_id}/events` 的 SSE 事件流等待结果。新接入的 Kakao Pay API 使用不同协议：一次提交多个 ChatGPT Access Token，获取 `batchId` 后轮询批次状态。

本次在保留旧提链接口和现有数据的前提下，新增可切换的 Kakao provider，并让用户能够在账号页直接选择服务、设置每批数量并启动提链。

## 目标

- 完整保留现有旧提链接口和 SSE 工作流。
- 新增 `kakao_batch` provider，对接 Kakao 异步批量 API。
- 在账号页提供直观的 provider 选择和启动入口。
- 支持用户手动勾选账号，并把 Kakao 任务按可配置的 `1～5` 个账号自动分批。
- 两个 provider 分别保存 API 地址和 CDK，避免切换时误用凭据。
- 复用现有账号资格筛选、任务状态、支付链接展示和失败原因展示。
- 不新增独立任务中心、取消功能或其他非必要操作。

## 非目标

- 不移除或改写旧 provider 的外部协议。
- 不迁移或删除已有账号的提链结果。
- 不让 Kakao provider 支持 PIX、UPI 或 IDEAL；它只生成 Kakao/Nicepay 支付链接。
- 不调用真实 CDK 完成自动化测试。

## Provider 模型

提链服务使用两个稳定标识：

- `legacy`：现有 `/api/extract` 加 SSE 事件流协议。
- `kakao_batch`：新的 `/api/v1/extractions/async` 加批次轮询协议。

`core/extract_link_service.py` 继续作为 WebUI 的统一入口，负责 provider 选择、账号占用、任务排队、状态更新和结果落库。旧协议实现保留现有行为；Kakao 协议放入独立客户端或 provider 模块，避免把两套网络协议继续混在同一执行函数中。

## 配置设计

### 通用配置

- `EXTRACT_LINK_PROVIDER`：默认 provider，取值 `legacy` 或 `kakao_batch`；默认 `legacy`，保证升级后不改变现有行为。
- `KAKAO_EXTRACT_BATCH_SIZE`：Kakao 默认每批数量，范围 `1～5`，默认 `5`。
- 现有 `EXTRACT_LINK_WORKERS` 和 `EXTRACT_LINK_QUEUE_LIMIT` 继续作为共享后台队列配置。

### 旧 provider

以下现有配置继续只服务于旧 provider，不改名，避免破坏已有 `.env`：

- `EXTRACT_LINK_API_BASE`
- `EXTRACT_LINK_CDK`
- `EXTRACT_LINK_TYPE`
- `EXTRACT_LINK_REQUEST_TIMEOUT`
- `EXTRACT_LINK_EVENT_TIMEOUT`

### Kakao provider

- `KAKAO_EXTRACT_API_BASE`：默认 `https://tiqu.dxmcs.xin`。
- `KAKAO_EXTRACT_CDK`：Kakao API 专用 CDK。
- `KAKAO_EXTRACT_TIMEOUT_SECONDS`：发送给服务端的批次运行上限，默认 `930`，范围 `30～1200`。
- `KAKAO_EXTRACT_POLL_INTERVAL`：轮询间隔，默认 `4` 秒，限制在合理范围内。

配置页将提链设置分成“通用配置”“旧接口”“Kakao API”三个区域。两个 CDK 字段分别遮挡显示和保存。账号页不直接展示 API 地址、CDK 或超时等高级参数。

## 账号页交互

现有“套餐/提链”工具栏调整为：

```text
查套餐  提链服务：[旧接口 ▼]  每批：[5]  提链  设为默认
```

交互规则：

- 页面加载时选择 `EXTRACT_LINK_PROVIDER` 中保存的默认 provider。
- 用户切换下拉框只影响当前页面后续操作。
- 点击“设为默认”时，同时保存当前 provider；当前选择为 Kakao 时也保存每批数量。
- “每批”输入仅在选择 Kakao provider 时启用或显示，范围为 `1～5`。
- 用户手动勾选哪些账号，就只处理这些账号；后台仍以资格和 Token 是否存在为最终校验。
- 批量按钮使用当前选择的 provider。每行原有“提链”按钮同样使用当前选择的 provider；单账号操作忽略每批数量。
- 启动前的确认框显示 provider、有效账号数、Kakao 每批数量和预计批次数。
- Kakao 的产品级每批上限固定为 5。即使上游接口文档允许更多，WebUI 和后端都只接受 `1～5`。
- 例如用户勾选 12 个有效账号且每批设置为 5，后台拆成 `5 + 5 + 2`。

账号表格继续使用现有提链状态区域，不新增页面。状态文案包含 provider，例如“Kakao·排队中”“Kakao·处理中”“提链成功(KAKAO)”和“提链失败”。

## Web API 变更

现有启动接口保持路径兼容：

- `POST /api/accounts/extract-link`
- `POST /api/accounts/extract-link-bulk`

请求体新增可选字段：

- `provider`：`legacy` 或 `kakao_batch`；缺省时使用保存的默认 provider。
- `batch_size`：仅 Kakao 批量操作使用，范围 `1～5`；缺省时使用保存的默认值。

批量接口继续接收用户勾选的 `account_ids`。响应增加 provider、有效账号数、跳过账号数和计划批次数，但不返回 Access Token 或完整 CDK。

新增轻量配置接口供账号页使用：

- 读取当前默认 provider、默认 Kakao 每批数量和可选 provider。
- 保存“设为默认”操作提交的 provider 和 Kakao 每批数量。

持久化继续使用项目现有 `.env` 配置保存机制，不另建数据库配置表。

## Kakao 数据流

### 账号准备

1. 根据用户提交的账号 ID 读取账号。
2. 跳过不存在、缺少 Access Token 或不满足 `free + plus_trial_eligible` 的账号，并为每个跳过项返回原因。
3. 在提交上游前按完整 Access Token 去重，同时保留每个 Token 对应的本地账号列表。
4. 按用户设置的 `1～5` 个唯一 Token 生成批次。

本地预先去重用于匹配上游“重复 Token 自动去重”的行为。若多个本地账号记录持有同一个 Token，只向上游提交一次，并将该 Token 的最终结果同步写回这些本地记录，避免结果数组错位和重复扣费。

### 异步提交

每批调用：

```http
POST /api/v1/extractions/async
Content-Type: application/json
```

请求结构：

```json
{
  "accessTokens": ["TOKEN_1", "TOKEN_2"],
  "cdk": "KAKAO_CDK",
  "timeoutSeconds": 930
}
```

成功响应必须包含 `batchId`。本地保存 `batchId`，并把本批账号更新为运行中。

批次提交不进行盲目自动重试：如果 POST 已被服务端接收但客户端在读取响应时超时，立即重提可能生成重复批次。此类不确定状态保留明确错误信息，交由用户确认后重试。只有确定请求未发出时才允许底层连接重试。

### 状态轮询

按照配置间隔调用：

```http
GET /api/v1/extractions/{batchId}
```

轮询直到：

- `done=true`；或
- `status` 为 `completed` 或 `error`；或
- 达到本地任务截止时间。

轮询 GET 遇到短暂网络错误、限流或服务端错误时进行有限次数退避重试。一次轮询失败不会立刻使整批失败。

### 结果映射

- 提交前保存“唯一 Token 顺序 → 本地账号 ID 列表”的批次清单。
- 默认按照 `results[]` 与提交 Token 的顺序一一映射。
- 返回中存在 `tokenHint` 时，用它辅助校验映射。
- 如果结果数量和提交的唯一 Token 数量不一致，或 `tokenHint` 明确冲突，不把后续结果错位写入账号；无法可靠匹配的条目标记为“服务端结果数量或顺序异常”。
- 每个 `results[].success` 独立决定账号成功或失败，允许同一批次部分成功。
- 成功结果保存 `paymentLink`；失败结果保存该条目的错误原因。
- `remainingCount` 和 `chargedCount` 保存为批次级元数据，并将最新的 `remainingCount` 用于前端展示。

## 状态与持久化

在现有提链字段基础上增加或统一写入：

- `extract_link_provider`
- `extract_link_batch_id`
- `extract_link_batch_number`：当前批次在本次操作中的序号，从 1 开始。
- `extract_link_batch_total`：本次操作拆出的批次数量。
- `extract_link_result_index`：该账号对应 `accessTokens[]` / `results[]` 的零基下标；重复 Token 的多个本地账号共享同一下标。
- `extract_link_status`
- `extract_link_message`
- `extract_link_url`
- `extract_link_error`
- `extract_link_cdk_remaining`
- 开始、更新和完成时间

旧 provider 可以继续写入现有 `job_id`；Kakao 使用 `batchId`。读取旧账号时缺少新字段视为 `legacy` 或未知来源，不进行数据迁移。

## CDK 用量展示

- 旧 provider 继续使用现有 CDK 主动查询接口。
- Kakao 文档未提供独立 CDK 查询接口，因此不伪造查询能力。
- Kakao 在任务响应包含 `remainingCount` 后展示“最近剩余次数”。尚未执行成功查询时显示“暂无用量记录”。
- `CDK_INVALID`、`CDK_QUOTA_EXHAUSTED` 和 `CDK_QUOTA_INSUFFICIENT` 转换为明确中文提示。

## 并发和队列

- 旧 provider 保持现有单账号后台任务行为。
- Kakao provider 以“批次”为执行单元，每批占用一个共享 executor worker。
- 所有批次使用现有队列容量限制，避免选择大量账号时创建无限后台任务。
- 账号在加入批次前原子占用；队列提交失败时释放已占用状态，允许用户重新启动。
- 同一个账号已经处于排队或运行状态时返回 busy，不重复提交。

## 重启恢复

- WebUI 启动时，旧 provider 继续使用现有中断恢复逻辑。
- Kakao 账号记录存在 `batchId` 且状态为排队或运行时，按 `batchId` 分组并恢复轮询；恢复后依靠持久化的 `extract_link_result_index` 把返回结果重新映射到账号，不需要保存完整 Token。
- 没有 `batchId` 的未完成 Kakao 任务标记为中断，可由用户重新启动。
- 已完成状态不重复查询或覆盖。

## 错误处理

- `CDK_INVALID`：CDK 无效、已停用或已过期。
- `CDK_QUOTA_EXHAUSTED`：CDK 次数已用完。
- `CDK_QUOTA_INSUFFICIENT`：CDK 剩余次数不足以覆盖本批唯一 Token。
- HTTP 422：显示服务端字段校验详情。
- HTTP 404 / `batch not found`：批次不存在或已被服务端清理。
- 轮询暂时失败：有限退避重试并保持运行状态。
- 服务端批次超时或最终错误：只失败当前批次，不影响其他批次。
- 部分成功：逐账号保存结果，不把整批统一标记为失败。
- 所有用户可见错误限制长度并隐藏 Token、CDK 和带认证信息的敏感内容。

## 测试策略

自动测试使用模拟 HTTP 响应，不消耗真实 CDK：

- 旧 provider 现有测试继续通过。
- 默认 provider 和 `1～5` 批量参数校验。
- 单账号 Kakao 提交和结果落库。
- 手动选择账号以及 `12 → 5 + 5 + 2` 分批。
- 多个本地账号持有重复 Token 时只提交一次并正确回写。
- 多批次、部分成功和逐账号失败原因。
- 返回数量不一致或 `tokenHint` 冲突时不发生结果错配。
- CDK 错误、HTTP 422、批次不存在、轮询重试和最终超时。
- POST 响应不确定时不盲目重复提交。
- WebUI 重启后按 `batchId` 恢复轮询。
- 两套 API 地址和 CDK 独立保存。
- provider 临时切换和“设为默认”。
- API 响应与前端页面均不泄露完整 Token 或 CDK。

实现完成后，在当前 `http://127.0.0.1:5002/` 验证账号勾选、provider 切换、每批数量、启动确认、状态变化和配置保存。真实 Kakao 提链只在用户明确提供测试 CDK 并要求执行时进行。

## 兼容性与发布方式

- 默认 provider 为 `legacy`，因此已有部署升级后继续走旧接口。
- 现有 `EXTRACT_LINK_*` 配置含义保持不变。
- 新字段均为可选字段，旧账号 JSON 可以直接读取。
- 前端未传 `provider` 时由服务端使用已保存默认值，旧调用方继续可用。
- 所有改动只在 `codex/hero-sms-provider` 分支和指定 worktree 内进行，不切换或合并 `main`，并保留未跟踪的 `logs/`。
