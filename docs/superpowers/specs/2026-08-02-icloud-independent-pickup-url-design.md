# iCloud 独立取件 URL 接入设计

**日期：** 2026-08-02
**分支：** `codex/hero-sms-provider`
**目标：** 在保留现有 iCloud Pickup/Profile 能力的同时，支持按邮箱绑定的独立 HTML 取件 URL，并确保不同取件来源、不同邮箱和不同注册任务之间不会串号。

## 1. 背景与范围

现有 iCloud 邮箱池主要接受 `邮箱 + Token`，可选 JSON Pickup API 地址。新上游提供的是每个邮箱独有的浏览器取件页面，导入形式为：

```text
邮箱---独立取件URL
邮箱----独立取件URL
邮箱-----独立取件URL
```

横线数量可能因上游导出误差而变化，因此三个及以上连续横线都视为有效分隔符。本次只修改 iCloud 邮箱素材的导入、保存、展示和验证码读取；不改变 Outlook、通用 API、HeroSMS 或注册浏览器流程。

## 2. 方案选择

### 采用：邮箱级来源绑定 + 独立页面适配器

每条邮箱记录保存自己的 Token、独立 URL 和取件模式。验证码读取始终从当前注册任务已经领取的邮箱上下文出发，不扫描其他邮箱，也不使用其他邮箱的凭据。

选择该方案的原因：

- 与现有 `ICloudMailAccount` 上下文和邮箱池领取机制兼容。
- 可以同时保留旧 Pickup/Profile 与新 HTML 页面。
- 来源选择是确定的，便于测试和排查串号。
- 不需要创建第二套邮箱池或改变注册任务的邮箱来源名称。

未采用的方案：

1. **把独立 URL 当成 Token。** 改动少，但会发送错误的 Authorization 头并回退到全局 Pickup，无法读取 HTML。
2. **建立完全独立的新邮箱来源。** 隔离更强，但会重复实现领取、释放、统计和 UI，当前规模下没有必要。

## 3. 导入解析

### 3.1 新格式

优先使用锚定解析规则：

1. 行首必须是合法邮箱，当前接受 iCloud 邮箱地址。
2. 邮箱结束后出现三个或更多连续 `-`，作为第一个分隔符。
3. 第一个分隔符后的内容若以 `https://` 开头，则识别为“仅独立 URL”。
4. 否则识别为 Token；若 Token 后还有三个或更多连续 `-` 且后面以 `https://` 开头，则识别为“Token + 独立 URL”。
5. 邮箱用户名或 URL 内部的普通横线不参与分隔，因为分隔位置分别锚定在邮箱结尾和 `https://` 之前。
6. URL 原样写入受保护的邮箱池记录，不对路径做拆分或重组。

以下格式均有效：

```text
name@icloud.com---https://HOST/show/CREDENTIAL/name@icloud.com
name@icloud.com----https://HOST/show/CREDENTIAL/name@icloud.com
name@icloud.com-----https://HOST/show/CREDENTIAL/name@icloud.com
name@icloud.com----TOKEN----https://HOST/show/CREDENTIAL/name@icloud.com
```

### 3.2 兼容旧格式

原有 Token 导入继续兼容 `----`、`====`、制表符、竖线、逗号和冒号。新解析器先尝试 iCloud 专用横线格式，未匹配时才进入旧格式解析，避免 URL 中的冒号被误切分。

### 3.3 有效性校验

- Token 和独立 URL 至少存在一个。
- 独立 URL 必须使用 HTTPS，且必须包含主机名。
- URL 路径末尾若包含邮箱，则解码后必须与行首邮箱一致；不一致的行计入 `invalid`，避免邮箱与取件页面错配。
- 同一批次重复邮箱采用最后一条有效材料，并将之前的重复项计入 `skipped`。
- 已存在的同邮箱记录只更新凭据，不创建第二条竞争记录；处于运行中的 `used` 或已完成的 `registered` 状态不被重置。

## 4. 数据模型与来源隔离

邮箱池记录继续使用现有 JSON 存储，新增或规范以下字段：

```text
email
token
pickup_url
pickup_mode
```

`pickup_mode` 取值：

- `api_token`：仅 Token，走现有 JSON Pickup，必要时使用同邮箱的 Profile 数据。
- `independent_url`：仅独立 URL，只读取该 URL，不回退到全局 Pickup/Profile。
- `independent_url_with_token`：独立 URL 优先；页面失败后，使用同一邮箱记录中的 Token，再按现有 Pickup → Profile 顺序读取。

旧记录缺少 `pickup_mode` 时按字段自动推导，不要求一次性迁移数据文件。

每次注册领取邮箱后，完整上下文缓存在 `ICloudMailAccount` 中。后续验证码轮询只接受该上下文对应的响应：

- 不扫描邮箱池中的其他记录。
- 不复用其他邮箱的 Token 或 URL。
- 不因全局 Profile 中出现其他邮件而切换目标邮箱。
- 任务结束释放缓存，避免后续任务复用旧上下文。

## 5. 独立 HTML 页面适配器

新增一个职责单一的页面解析路径，与现有 JSON Pickup 解析分离：

1. 使用该邮箱绑定的 URL 发起 GET，请求头接受 HTML，沿用现有取件超时。
2. 请求时给 URL 增加或更新 `n=10` 查询参数，以读取最近邮件；保留原路径、凭据和其他查询参数。
3. 200 响应按 HTML 页面处理；空邮箱页面返回“暂无新邮件”，继续正常轮询。
4. 从页面中的邮件条目提取收件人、发件人、主题、时间和正文文本。HTML 实体解码后复用现有 `looks_like_openai_email()` 与 `extract_otp()`。
5. 只接受与当前邮箱匹配、时间不早于本次验证码请求容差范围、且符合 OpenAI 验证邮件特征的验证码。
6. 多封邮件按时间从新到旧检查；旧码、其他服务邮件和其他邮箱邮件全部跳过。
7. 页面结构无法识别时返回脱敏错误，不把完整 URL 或路径凭据写入异常和日志。

页面会自行每五秒刷新，但客户端不依赖浏览器刷新标签；轮询由现有 `fetch_latest_otp()` 控制。

## 6. 回退与错误处理

来源顺序必须由单条邮箱记录决定：

```text
api_token:
  JSON Pickup -> 同邮箱 Profile（配置后）

independent_url:
  独立 HTML URL

independent_url_with_token:
  独立 HTML URL -> 同邮箱 JSON Pickup -> 同邮箱 Profile（配置后）
```

错误策略：

- 独立页面返回空邮箱或尚无新码：继续轮询。
- 401/403：标记该邮箱材料失效；错误信息不含凭据。
- 429：尊重 `Retry-After`，沿用现有退避上限。
- 5xx 或网络异常：记录当前来源的脱敏状态；混合模式可尝试同邮箱后备来源。
- 仅 URL 模式不访问全局 Pickup/Profile，避免来源混乱。
- 所有可进入日志的异常先移除 Token、完整 URL、URL 查询和凭据路径。

## 7. UI 与脱敏

- 导入说明更新为“iCloud API：邮箱 + Token，或邮箱 + 独立取件 URL；横线分隔符三个及以上均可”。
- 邮箱池列表展示取件模式，例如“API Token”“独立 URL”“URL + API 后备”。
- 列表 API 不返回原始 `pickup_url`；只返回 `has_pickup_url`、`pickup_mode` 和不含凭据的来源提示。
- `copy_line` 保持仅邮箱，不包含 Token 或 URL。
- Token 继续使用现有掩码；URL 路径和查询中的凭据不在 UI、日志或普通 API 响应中出现。
- 原始 Token 和 URL 只保存在邮箱池内部数据以及运行任务的内存上下文中。

## 8. 测试设计

### 导入与存储

- 三、四、五及更多横线均能导入 URL-only 行。
- 邮箱用户名含横线、URL 路径含横线时保持完整。
- Token-only、Token + URL 和旧分隔符继续有效。
- URL-only 不再因 Token 为空而被判无效。
- URL 末尾邮箱不匹配时拒绝导入。
- 重复邮箱更新同一记录，不产生两个可领取项。
- 列表 API、`copy_line` 和错误输出不包含 URL 凭据。

### 页面读取

- 请求 URL 正确追加 `n=10`，且不破坏原查询参数。
- 空邮箱 HTML 继续轮询。
- 从主题、正文和 HTML 内容中提取六位验证码。
- 多封邮件选择当前邮箱最新的 OpenAI 验证码。
- 跳过旧邮件、其他邮箱邮件和非 OpenAI 邮件。
- HTML 结构异常、网络错误和 HTTP 错误均不泄露 URL 凭据。

### 来源隔离与回归

- URL-only 邮箱不调用 JSON Pickup/Profile。
- 混合邮箱只回退到同一邮箱的 Token/Profile。
- 两个并发邮箱分别使用自己的 URL，不会串码。
- 现有 iCloud Pickup/Profile、邮箱池状态、WebUI 导入、HeroSMS 和完整测试套件继续通过。

## 9. 完成标准

1. 用户给出的独立 URL 格式可以直接批量导入，三个及以上横线都有效。
2. URL-only 邮箱能独立轮询页面并提取当前注册请求的验证码。
3. Cloud API、独立 URL 和混合记录严格按邮箱绑定，不会跨邮箱或跨来源取码。
4. 页面、API、复制内容、日志和异常中不出现独立 URL 的凭据。
5. 针对性测试和完整回归测试通过，`logs/`、`.env` 和真实凭据不进入提交。
