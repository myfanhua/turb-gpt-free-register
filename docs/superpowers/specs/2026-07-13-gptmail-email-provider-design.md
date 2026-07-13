# GPTMail 邮箱来源设计

## 目标

将 GPTMail（`https://mail.chatgpt.org.uk`）作为新的自动注册邮箱来源接入。它必须能按需生成临时邮箱、轮询新邮件并提取 OpenAI 的六位验证码；Web 配置页仅暴露 GPTMail API Key。

## 范围

- 新增邮箱来源标识 `gptmail`，可单独使用，也可在 `EMAIL_SOURCE` 中与现有来源按顺序兜底。
- 固定调用 GPTMail 的公开服务地址；不提供 Base URL、邮箱前缀或域名配置。
- 使用 `POST /api/generate-email` 生成随机邮箱，使用 `GET /api/emails?email=...` 查询收件箱，并以 `GET /api/email/{id}` 获取正文。
- 按邮箱领取后开始的时间过滤旧邮件，使用项目既有 OTP 抽取逻辑从主题、纯文本或 HTML 正文中取得六位验证码。
- 在 Web 的“邮箱 / OTP”配置组增加一个隐藏字段“GPTMail API Key”，保存到 `.env` 的 `GPTMAIL_API_KEY`，运行时热加载。
- 当 `EMAIL_SOURCE` 含有 `gptmail` 但 API Key 为空时，不发出远端请求；注册任务返回明确错误“请填写 GPTMail API Key”。

## 非目标

- 不使用 GPTMail 公共测试 Key，不自动回退到该 Key。
- 不管理或删除 GPTMail 服务端邮件。
- 不在本地邮箱池中预导入或持久化 GPTMail 地址；地址由运行时即时生成。
- 不改变 Outlook、通用 API 邮箱或 Cloudflare 域名邮箱的现有行为。

## 组件与数据流

1. `config/email.py` 定义 `GPTMAIL_API_KEY` 的默认空值，并从 `.env` 覆盖。
2. `core/gptmail_client.py` 负责验证 Key、调用 GPTMail API、缓存当前任务的邮箱上下文，以及轮询邮件和提取 OTP。
3. `core/email_provider.py` 将 `gptmail` 纳入来源解析、领取、来源识别、OTP 等待和回收流程。GPTMail 的回收只清理内存上下文，因为服务端地址无需本地池状态。
4. `webui/config_editor.py` 将 API Key 加入“邮箱 / OTP”白名单，标记为机密并写入 `.env`；现有配置页会自动渲染该字段。
5. `.env.example` 和 `README.md` 说明如何配置 `GPTMAIL_API_KEY`，以及将 `gptmail` 加入 `EMAIL_SOURCE`。

## API 契约与错误处理

所有请求携带 `X-API-Key` 和接受 JSON 的请求头，客户端设定超时并将网络、HTTP、无效 JSON 与 `success: false` 统一转换为包含操作上下文的 `GPTMailError`。

- 生成邮箱：仅接受成功响应中非空的 `data.email`。
- 收件箱：仅接受 `data` 中的邮件数组；会兼容常见的 `data.emails` 结构。
- 邮件详情：从 `data` 读取主题、文本和 HTML 等字段并交给现有 `extract_otp`。
- 轮询：沿用全局 `OTP_MAX_WAIT`、`OTP_POLL_INTERVAL` 和稳定窗口设置；只接受领取时间之后到达的邮件，避免读取旧验证码。
- 缺少 Key：在首次领取时立即失败，错误文本可直接显示给 Web 任务日志与 CLI 用户。

## 验证策略

新增单元测试并先观察失败，覆盖：来源解析、缺 Key 报错、生成邮箱请求、响应错误、仅从领取后新邮件提取 OTP，以及多来源回收路由。HTTP 调用以可注入/模拟的会话隔离，OTP 提取本身仍走真实项目工具函数。实现后运行相关测试和项目可用的完整测试命令。
