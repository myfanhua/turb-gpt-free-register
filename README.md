# ChatGPT 账号资产与会话运营控制台

本地控制台用于管理 ChatGPT 账号资产的注册、邮箱取码、账号池、受控导出与 Conversation Pool。默认主线是 protocol 注册：复用 `BrowserSession/curl_cffi` 的 TLS 指纹、登录态与 access token；Roxy、Cloak、Browser Use、Skyvern 是可选注册驱动。

Conversation Pool 默认关闭。启用前必须先用脱敏 HAR 验证网页 conversation 协议与 SSE；当前不会自动发送会话消息。

启用时协议 adapter 复用项目的 `BrowserSession/curl_cffi`、保存的 access token/cookies 与 Sentinel prepare/finalize，再向普通 ChatGPT conversation SSE 路由发送文本。应先以用户授权的测试账号发送一条非破坏性消息，验证登录态、反滥用挑战和 SSE；不要在源码或 `.env` 中保存真实凭证。

若 `chat-requirements` 明确要求 Turnstile，程序会保存脱敏的 `stage`、`http_status` 与 `reason` 后停止，绝不会伪造或绕过挑战。只能在同一账号、同一已验证的浏览器上下文中由用户完成一次真实验证，再恢复协议会话。

文本 conversation adapter 的协议字段参考已跟踪的 `chatgpt2api` 协议代码；本项目不依赖其运行时、账号池、图片能力或 Web 服务。

## 注册与资产能力

### 显示名称

`REGISTER_NAME` 留空时，CLI 和 WebUI 使用相同的名称生成器。`REGISTER_NAME_LOCALE=ja`（默认）生成 `Haruto Sato` 一类日式罗马字姓名；不会提交日文汉字或假名，且始终满足注册接口仅接受 ASCII 字母和空格的限制。当前仅支持 `ja`。

- **protocol**：原纯协议注册，基于 `curl_cffi` + Sentinel/PoW。
- **roxy**：RoxyBrowser 指纹浏览器 + Selenium 自动化注册，兼容新版页面流，例如 `create-account/password`、`about-you` 年龄/生日表单、地区本地化页面等。
- **cloak**：CloakBrowser + Playwright 适配层自动化注册，支持免费 binary、无头模式、humanize、固定 fingerprint seed、代理 geoip。
- **browser_use**：Browser Use Cloud stealth Chromium + Playwright（可选住宅代理，无需本机安装 Roxy）。
- **skyvern**：Skyvern Browser Sessions 云端浏览器 + Playwright CDP。

项目提供 **CLI** 和 **本地 WebUI** 两种使用方式。

> 项目说明：本项目基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 进行改造与扩展。

> 开源版说明：仓库只保留源码、配置模板和文档；运行时账号、Token、邮箱池、Codex 凭证、日志等真实数据均已通过 `.gitignore` 排除。

---

## 功能概览

### 免手机验证 Synthetic 转化（新）

- 学习自 [zhishile/codex-auth-helper](https://github.com/zhishile/codex-auth-helper) 的原理：注册拿到的 ChatGPT 网页 session accessToken 本身就是凭证，本地构造 `alg=none` 的 synthetic id_token（header 带 `cpa_synthetic: true`），即可组装标准 Codex `auth.json`（`auth_mode: "chatgpt"`），**跳过需要接码的 Codex OAuth 手机验证**。
- 纯本地转化，零网络请求、零风控增量；一键产出单账号 auth.json、Sub2API 导入文件、Cockpit 导入文件，可选直传 sub2api（codex-session 导入接口）。
- 开关：`CODEX_SYNTHETIC_AUTH_ENABLE`（WebUI「配置 → 功能开关 → 免手机验证转化」）。开启后注册成功自动转化并跳过 Codex OAuth；关闭则走原接码流程。
- WebUI 账号资产页支持单个/批量「一键转化」「转化并传sub2」，带结果面板与下载链接。
- 固有限制：synthetic id_token 无签名，仅适用于不验签的下游（Codex CLI chatgpt 模式 / sub2api session 导入 / Cockpit）；refresh_token 为 sessionToken 或占位值，access token 过期后需重新转化。

### 会话池五轮对话（新）

- 账号绑定消息模板后逐条对话（发一条、等 SSE 答完、再发下一条），后台线程池执行，checkpoint 持久化，支持断点续跑与显式重试。
- 开关：`CONVERSATION_POOL_ENABLE`（注册后自动绑定默认模板）+ `PROTOCOL_CONVERSATION_ENABLE`（真实发送总开关），WebUI「配置 → 会话池」可视化配置。
- 风控抑制：注册后随机延迟 45-180 秒启动（手动运行 2-8 秒抖动），消息间随机间隔 8-25 秒（均可配置）；复用注册时的代理/浏览器画像/cookies，最多 2 个绑定并发。
- WebUI 会话池页提供模板 CRUD、账号绑定、运行/重试/解绑/全部排队、进度与脱敏错误实时轮询；内置一键生成默认五轮中文模板。
- 遇到 Turnstile 时绝不伪造或绕过，标记 `needs_browser_verification` 并保存脱敏诊断，需在已验证浏览器上下文完成真实验证后显式重试。


### 注册

- 批量注册 ChatGPT 账号。
- 支持注册驱动切换：
  - `REGISTRATION_DRIVER = "protocol"`
  - `REGISTRATION_DRIVER = "roxy"`
  - `REGISTRATION_DRIVER = "cloak"`
  - `REGISTRATION_DRIVER = "browser_use"`
  - `REGISTRATION_DRIVER = "skyvern"`
- 支持 RoxyBrowser 一号一环境：自动创建、打开、关闭、删除 Roxy Profile。
- 支持 Roxy 无头启动：`ROXY_OPEN_HEADLESS=True`。
- 支持 CloakBrowser：免费 binary、无头模式、humanize、固定 fingerprint seed、按出口 IP 自动匹配语言/时区/WebRTC。
- Roxy / Cloak 浏览器注册已兼容：
  - 填邮箱后直接进入邮箱验证码页；
  - 填邮箱后先进入 `create-account/password`，自动设置密码再继续；
  - `about-you/profile` 页面直接输入年龄数字；
  - `about-you/profile` 页面输入年月日生日；
  - React Aria birthday select / spinbutton 年月日控件；
  - 不同出口 IP / 不同页面语言下按钮顺序变化导致的三方登录误点问题。

### 邮箱来源

支持多种邮箱来源：

- Outlook 邮箱池：`email----password----clientId----refreshToken`
- Cloudflare 域名邮箱 + QQ 邮箱 IMAP 收信（`cloudflare_domain`）
- Cloudflare Worker 临时邮箱：自动创建 + JWT 取码（`cloudflare`，兼容 cloudflare_temp_email）
- 通用 API 邮箱：`email----取码地址`
- GPTMail 临时邮箱 API：运行时随机生成邮箱并自动收取验证码
- `EMAIL_SOURCE` 支持多个来源组合，例如：

```python
EMAIL_SOURCE = "outlook,generic_api"
```

- MailNest-迈巢：Outlook 临时邮箱
- Assurivo 本地素材池：`email----查询码`，通过 Assurivo 控制台查询验证码
- 通用 API 邮箱也支持直接导入 `email----https://assurivo.com/console/open.php?mail=...&pwd=...&limit=5`。URL 原样受控保存，`mail` 与左侧邮箱不一致会拒绝导入；它属于敏感资产，仅能经明确复制或完整资产导出操作取得。

### Codex OAuth

- 注册成功后可自动跑 Codex OAuth。
- Codex 授权驱动可选：
  - `CODEX_OAUTH_DRIVER = "protocol"`
  - `CODEX_OAUTH_DRIVER = "roxy"`
  - `CODEX_OAUTH_DRIVER = "cloak"`
  - `CODEX_OAUTH_DRIVER = "browser_use"`
  - `CODEX_OAUTH_DRIVER = "same_as_registration"`
- 支持 CPA 管理接口生成授权 URL，并提交 OAuth callback。
- 支持接码平台：
  - GrizzlySMS
  - 本地 L 取号服务，见 `L_API.md`
- 手机验证支持自动取号、填号、收码、提交、失败换号重试。
- Codex 凭证落盘到 `codex_accounts/`。

### WebUI

- 批量启动注册任务。
- 实时查看任务日志。
- 动态调整注册线程数，提交后新任务立即使用最新值。
- 批量补跑 Codex，补跑线程数每次提交即时生效。
- 管理账号、邮箱池、Codex 凭证；账号页支持复制全部/选中整行，邮箱池列表展示导入时间、已用时间和状态。
- 配置页支持热加载，保存后无需重启。
- Roxy 团队/项目可在配置页获取并保存。

---

## 环境要求

- Python 3.10+
- Node.js 18+
- 可用代理、系统代理/VPN，或 RoxyBrowser 代理环境
- 如使用 Roxy 注册：需要本机 RoxyBrowser API 可访问
- 如使用 Cloak 注册：首次运行会自动下载 Cloak Chromium binary；`CLOAK_GEOIP=True` 需要 `cloakbrowser[geoip]` 依赖
- 如启用 Codex 自动授权：需要接码平台配置

安装依赖：

```bash
pip install -r requirements.txt
node --version
```

### 密钥配置（.env）

重要 API Key 请放在项目根目录 `.env`，不要写进 `config/*.py`。

```bash
cp .env.example .env
# 编辑 .env，例如：
# BROWSER_USE_API_KEY=...
# ROXY_API_TOKEN=...
```

当前支持从 `.env` 读取的密钥：

- `WEBUI_AUTH_CODE`（WebUI 登录授权码）
- `WEBUI_SESSION_SECRET`（可选，Session Cookie 签名密钥）
- `BROWSER_USE_API_KEY`
- `SKYVERN_API_KEY`
- `ROXY_API_TOKEN`
- `QQ_IMAP_PASSWORD`
- `CLOUDFLARE_API_KEY` / `CLOUDFLARE_CUSTOM_AUTH`（`EMAIL_SOURCE=cloudflare` 时）
- `CPA_MANAGEMENT_KEY`
- `SMS_API_KEY`
- `L_ADMIN_AUTH_CODE`
- `H_ADMIN_AUTH_CODE`

WebUI 配置页保存这些字段时会写入 `.env`（不是 config 源码）。

### 账号导出：Sub2API / Cockpit

账号页选中账号后可点击“导出 Sub2API”或“导出 Cockpit”。文件写入项目根目录 `exports/`，并通过受控下载接口下载；该目录已被 Git 忽略，请按凭证文件妥善保管，勿上传或分享。

- Sub2API 始终导出一个 `{ exported_at, proxies: [], accounts: [] }` 文档。
- Cockpit 选中一个账号时导出单对象；选中多个账号时导出对象数组。
- 导出只转换已保存账号，不会发起登录、刷新或修改账号状态。
- 导出是静态快照，不代表 Token 会永久可用。Sub2API/Cockpit 保留 `expires_at`、`missing_credentials`、`refreshable` 与 `snapshot_only` 元数据；`refreshable=true` 仅表示明确保存了 OpenAI OAuth `refresh_token`，绝不把 Outlook 邮箱 OAuth 资产误认成 ChatGPT 刷新凭证。
- 已保存 ChatGPT Web session/Cookie 的账号会显示“可尝试会话续期”。该操作必须由用户在 WebUI 中逐账号显式触发，只请求官方 `/api/auth/session`；遇到 401、Cloudflare 或 Turnstile 会停止并报告，绝不绕过验证。它不是 OAuth 自动刷新，也不会由导出器自动执行。
- ChatGPT Web session 通常没有真实 OAuth `refresh_token` 或 `id_token`。缺失时会保留空字符串并在 `missing_credentials` 中提示；程序绝不会合成 JWT 或伪造认证材料。
- 注册流程仅在当前会话实际可取得时保存 `extra_json.auth_artifacts`（注册密码、access token、session、cookies）。旧账号缺字段仍可导出，但会有缺失提示；Outlook 素材密码不会被当作 OpenAI 注册密码。
- “完整账号资产 JSON”导出会额外包含 `email_asset`（实际可得的邮箱 provider、凭证与 Assurivo 查询入口）及 `missing_fields`。这是敏感凭证文件：API 响应不回显内容，只能通过受控下载取得；请妥善保管，勿上传或分享。

---

## 快速开始

### WebUI 授权码

WebUI 启动后，除 `/login` 外所有页面和 `/api/*` 接口都会校验授权码。推荐在 `.env` 中配置：

```dotenv
WEBUI_AUTH_CODE=你的授权码
```

也可以启动时直接传入：

```bash
python web.py --auth-code 你的授权码
```

优先级：`--auth-code` > `.env`/环境变量。若都未设置，启动时会在日志中生成并打印本次临时授权码。接口调用可使用登录后的 Cookie，或传 `X-Auth-Code: <授权码>` / `Authorization: Bearer <授权码>`。

`WEBUI_SESSION_SECRET` 可选；未设置时会从固定授权码派生稳定的 Session 签名密钥，修改授权码后已有登录会自动失效。

### 1. 配置邮箱源

#### Outlook 邮箱池

复制示例文件：

```bash
cp 用于注册的邮箱.txt.example 用于注册的邮箱.txt
```

每行格式：

```text
email----password----clientId----refreshToken
```

也可以在 WebUI 的「邮箱池」页面导入。

#### 通用 API 邮箱

每行格式：

```text
email----code_url
```

在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "generic_api"
```

或使用组合来源：

```python
EMAIL_SOURCE = "outlook,generic_api,mailnest"
```

#### GPTMail 临时邮箱

在 WebUI 的「配置 → 邮箱 / OTP」填写 `GPTMail API Key`，然后将邮箱来源设置为：

```python
EMAIL_SOURCE = "gptmail"
```

也可以在项目根目录 `.env` 中填写：

```dotenv
GPTMAIL_API_KEY=你的_GPTMail_API_Key
```

服务地址固定为 `https://mail.chatgpt.org.uk`。未填写 Key 时，任务会提示填写 `GPTMail API Key`，不会使用公共测试 Key。

#### Assurivo 邮箱池（`assurivo`）

在项目根目录创建 `用于注册的Assurivo邮箱.txt`，每行严格为：

```text
email----查询码
```

启用方式：

```dotenv
EMAIL_SOURCE=assurivo
# 或按顺序兜底：EMAIL_SOURCE=outlook,assurivo
```

程序会在同目录维护 `用于注册的Assurivo邮箱.json` 的领取状态；该状态与 Outlook、通用 API 邮箱池完全独立。查询码只作为 Assurivo 请求的 `pwd` 参数，不能当作邮箱密码，也不要粘贴进日志、截图或公开资料。

客户端只接受带 OpenAI/ChatGPT 身份和验证语义的六位验证码邮件，并依据邮件来源时间过滤早于本轮 `after_ts` 的内容。Assurivo 返回中缺少可靠时间字段的 HTML/文本会被保守拒绝，宁可等待超时也不会冒险使用缓存旧码；真实接口字段变化时请提供脱敏样本以完善兼容。

#### Cloudflare Worker 临时邮箱（`cloudflare`）

兼容 `cloudflare_temp_email` 类 Worker：注册时自动创建域名邮箱，并用 JWT 轮询收件箱提取 OpenAI 六位验证码。  
（与下方 `cloudflare_domain` / QQ IMAP 方案不同，请勿混用标识。）

```dotenv
EMAIL_SOURCE=cloudflare
CLOUDFLARE_API_BASE=https://你的-worker-api-域名
CLOUDFLARE_API_KEY=你的_ADMIN_PASSWORD
CLOUDFLARE_AUTH_MODE=x-admin-auth
# admin 创建时常用：
# CLOUDFLARE_PATH_ACCOUNTS=/admin/new_address
CLOUDFLARE_DEFAULT_DOMAINS=你的收信域名.com
```

匿名模式可将 `CLOUDFLARE_AUTH_MODE=none` 且 Key 留空，创建路径默认 `/api/new_address`；若被 Turnstile 拦截请改用 admin 模式。更多字段见 WebUI「配置 → 邮箱 / OTP」或 `.env.example`。

#### Cloudflare 域名邮箱（`cloudflare_domain`）

在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "cloudflare_domain"
EMAIL_DOMAIN = "你的域名"
QQ_EMAIL = "你的QQ邮箱"
QQ_IMAP_PASSWORD = "QQ邮箱IMAP授权码"
```

Cloudflare Email Routing 需要把域名邮件转发到 QQ 邮箱。此模式不调用 Worker 创建接口，仅本地生成地址并通过 QQ IMAP 取件。

#### MailNest-迈巢 Outlook 临时邮箱

可直接在 Web-UI 中配置 API Key 与项目代码`MAIL_NEST_PROJECT_CODE`，也可以在配置文件中配置。

- `api-key`获取页面：https://mailnest.top/account
- 项目代码获取页面：https://mailnest.top/buy-email。默认为`chatgpt001`，可以直接使用

---

### 2. 配置注册驱动

编辑 `config/roxybrowser.py`，或直接在 WebUI「配置」页修改。

#### 使用 RoxyBrowser 注册

```python
REGISTRATION_DRIVER = "roxy"  # 可选 protocol / roxy / cloak
ROXY_API_BASE = "http://127.0.0.1:50100"
ROXY_API_TOKEN = "你的Roxy API Key"
ROXY_WORKSPACE_ID = "你的workspaceId"
ROXY_PROJECT_ID = "你的projectId"
ROXY_ONE_PROFILE_PER_ACCOUNT = True
ROXY_DELETE_PROFILE_AFTER_RUN = True
ROXY_CREATE_USE_PROXY_POOL = True
```

如要无头：

```python
ROXY_OPEN_HEADLESS = True
```


#### 使用 CloakBrowser 注册

如需改用 CloakBrowser，先安装依赖：

```bash
pip install -r requirements.txt
```

然后在 `config/roxybrowser.py` 或 WebUI 配置页把注册驱动改为：

```python
REGISTRATION_DRIVER = "cloak"
```

再在 `config/codex.py` 或 WebUI「CPA / Codex」分组设置 Codex 授权驱动：

```python
CODEX_OAUTH_DRIVER = "same_as_registration"  # 跟随注册驱动
# 或单独指定："protocol" / "roxy" / "cloak" / "browser_use"
```

CloakBrowser 专用配置在 `config/cloakbrowser.py`：

```python
CLOAK_HEADLESS = False          # True=无头；False=显示窗口
CLOAK_HUMANIZE = True           # 人工鼠标/键盘/滚动行为
CLOAK_GEOIP = True              # 按当前出口 IP 自动匹配语言/时区/WebRTC
CLOAK_LOCALE = ""               # 留空自动；也可强制如 ja-JP / en-US
CLOAK_TIMEZONE = ""             # 留空自动；也可强制如 Asia/Tokyo
CLOAK_LICENSE_KEY = ""          # 留空使用免费 binary；填 Pro key 使用最新版
CLOAK_FINGERPRINT_SEED = ""     # 留空每次随机；固定值=固定指纹
CLOAK_USER_DATA_DIR = ""        # 留空临时环境；填路径可持久化 profile
```

说明：

- `CLOAK_GEOIP=True` 会按当前出口 IP 自动生成 `locale / timezone / Accept-Language`，并传给 CloakBrowser 与 Playwright context。
- 如果你通过项目代理池使用代理，请在 `config/proxy.py` 的 `PROXY_POOL` 填写代理；如果你使用系统代理/VPN，也会按当前实际出口 IP 自动定位。
- 免费版没有在项目侧限制窗口数；本项目每个注册任务会启动一个 CloakBrowser 实例，即一个实例一套指纹。
- WebUI 中，`Codex授权驱动` 位于「CPA / Codex」分组，对应 `config/codex.py` 的 `CODEX_OAUTH_DRIVER`。

#### 使用协议注册

```python
REGISTRATION_DRIVER = "protocol"
```

协议注册会使用 `curl_cffi`、Sentinel/PoW、代理池等配置。

#### 使用 Browser Use Cloud 注册

```python
REGISTRATION_DRIVER = "browser_use"
```

并在 `config/browser_use.py` 或 WebUI「配置 → Browser Use」填写：

```python
BROWSER_USE_API_KEY = "你的 Browser Use API Key"
BROWSER_USE_PROXY_COUNTRY_CODE = "jp"   # 可选：us/sg/de...
BROWSER_USE_USE_PROXY = True
BROWSER_USE_FAST_MODE = True       # 推荐开启：减少 Browser Use 额外等待
BROWSER_USE_LOG_TIMING = True      # 输出阶段耗时日志，方便定位慢点
BROWSER_USE_SESSION_TIMEOUT = 240  # Browser Use keepAlive/timeout，单位分钟；创建远端浏览器时保持活跃更久
```

如希望注册成功后也用 Browser Use 自动跑 Codex OAuth：

```python
ENABLE_CODEX_AUTO = True
CODEX_OAUTH_DRIVER = "browser_use"
# 或 CODEX_OAUTH_DRIVER = "same_as_registration"，当 REGISTRATION_DRIVER="browser_use" 时自动跟随
```

依赖：

```bash
uv pip install playwright --python .venv/bin/python
# 或
pip install playwright
```

说明：

- Browser Use 走远端 stealth Chromium，通过 Playwright `connect_over_cdp` 控制。
- `BROWSER_USE_SESSION_TIMEOUT=240` 会在 Browser Use 创建/连接远端浏览器时设置较长 keepAlive（connect URL 的 `timeout` 参数，单位分钟），避免等待邮箱 OTP、短信或 callback 时云端会话提前回收；代码会限制到 `1~240`。
- 如果第一次进入邮箱验证码页且邮箱里实际已有验证码，但程序没取到，通常是 Outlook 取件链路抖动：Graph TLS/REST/IMAP 某一轮失败、短轮询切片过短、或 `after_ts` 过滤边界过紧。Browser Use 驱动已放宽 Outlook 单轮取件切片、提前记录验证码过滤时间，并会在等待邮箱 OTP 超时后尝试点击重发继续等待；重发入口使用 DOM 结构/位置/属性启发式定位，不依赖页面文案或 OCR/文字识别。可在「邮箱 / OTP」把 `OTP_MAX_WAIT` 调大到 `180~240`，`OUTLOOK_FETCH_MODE` 优先用 `auto`。
- Outlook 取件日志会显示验证码来源：`source=graph`、`source=outlook_rest`、`source=imap_new`、`source=imap_entra_outlook`、`source=remote_graph` 或 `source=remote_imap`，便于判断是哪条链路成功取码。
- `BROWSER_USE_FAST_MODE=True` 会跳过大部分人工节奏等待；`BROWSER_USE_LOG_TIMING=True` 会打印连接、打开页面、邮箱、OTP、手机、callback 等阶段耗时。
- 支持作为 Codex OAuth 授权驱动：`CODEX_OAUTH_DRIVER="browser_use"`，可完成授权页面、邮箱 OTP、手机短信验证与 callback 捕获。
- 适合不想安装本机 Roxy、又想要 session 隔离 + 云端代理的场景。
- 免费额度/并发以 Browser Use 官方定价页为准。

---

### 3. 配置代理

编辑 `config/proxy.py`：

```python
PROXY_POOL = [
    "http://user:pass@host:port",
]
```

Roxy 一号一环境开启 `ROXY_CREATE_USE_PROXY_POOL=True` 时，会从这里随机取代理写入 Roxy Profile。

---

### 4. 配置 Codex OAuth

如不需要 Codex，关闭：

```python
ENABLE_CODEX_AUTO = False
```

如需要自动授权：

```python
ENABLE_CODEX_AUTO = True
# config/codex.py
CODEX_OAUTH_DRIVER = "browser_use"  # 可选 protocol / roxy / cloak / browser_use / skyvern / same_as_registration
```

接码配置在 `config/codex.py`：

```python
SMS_PROVIDER = "l"        # 可选 grizzly / l / h
SMS_API_KEY = "你的 GrizzlySMS key"  # 仅 GrizzlySMS 需要
SMS_SERVICE = "openai"
SMS_COUNTRY = "国家代码"
SMS_MAX_RETRIES = 10
SMS_CODE_WAIT = 120
SMS_POLL_INTERVAL = 5

# 若 SMS_PROVIDER="h"，H 固定复用：
#   SMS_SERVICE -> H projectId
#   SMS_COUNTRY -> H country
H_API_BASE = "http://localhost:8788"
H_ADMIN_AUTH_CODE = "你的H后台授权码"
```

CPA 授权地址来源：

```python
CODEX_AUTH_URL_SOURCE = "cpa"
CPA_MANAGEMENT_URL = "你的CPA管理地址"
CPA_MANAGEMENT_KEY = "你的CPA管理密钥"
```

---

## 使用方式

## WebUI 推荐方式

启动：

```bash
python web.py --open-browser
```

默认地址：

```text
http://127.0.0.1:5000
```

可指定端口：

```bash
python web.py --port 8000 --open-browser
```

允许局域网访问：

```bash
python web.py --host 0.0.0.0 --port 5000
```

WebUI 页面说明：

| 页面 | 功能 |
|---|---|
| 注册 | 设置注册数量、线程数，启动批量注册，查看任务和日志 |
| 账号 | 查看账号、复制 token、补跑 Codex、批量删除账号 |
| Codex 授权 | 查看/下载/删除 `codex_accounts/` 凭证 |
| 邮箱池 | 导入邮箱、筛选来源、标记可用/失败、删除邮箱 |
| 配置 | 修改运行配置并热加载，含 Roxy、Codex、邮箱、代理、人工节奏等 |

### 线程数说明

- 注册线程数在每次点击「开始注册」时读取。
- 如果线程数和上次不同，新提交任务会使用新线程池。
- 旧线程池里已经排队/运行的任务会继续跑完，不会被强制取消。
- Codex 批量补跑每次都会按本次提交的补跑线程数创建独立线程池。

---

## CLI 使用方式

注册 1 个：

```bash
python main.py
```

批量注册 10 个，3 线程：

```bash
python main.py -n 10 --workers 3 --continue-on-fail
```

详细日志：

```bash
python main.py -n 1 --verbose
```

参数：

| 参数 | 说明 | 默认 |
|---|---|---|
| `-n, --count` | 注册数量 | 1 |
| `--workers` | 并发线程数 | 1 |
| `--delay` | 每次注册结束后的间隔秒数 | 0 |
| `--continue-on-fail` | 单个失败后继续 | False |
| `--verbose` | DEBUG 日志 | False |

---

## Codex 补跑

WebUI 账号页可单个或批量补跑 Codex。

CLI 单独补跑：

```bash
python tools/test_codex_oauth.py --email <已注册邮箱> --verbose
```

补跑会消耗：

- 1 次邮箱 OTP
- 1 个接码号码

补跑日志在：

```text
注册日志/codex-retry-邮箱.log
```

---

## 注册密码说明

Roxy 注册如果遇到新版流程：

```text
/create-account/password
```

会自动设置密码。

密码来源：

1. 优先使用 `config/register.py`：

```python
REGISTER_PASSWORD = "你的固定密码"
```

2. 如果为空，自动生成 14 位强密码，包含大写、小写、数字、符号。

保存位置：

- 账号 `extra_json.registration_password`
- 批次归档 `accounts/YYYYMMDD-.../注册成功账号.json` 的 `extra.registration_password`

注意：账号表里的 `password` 字段仍用于 Outlook 邮箱素材密码，不会被 OpenAI 注册密码覆盖。

---

## 重要配置文件

| 文件 | 说明 |
|---|---|
| `config/roxybrowser.py` | 注册驱动、Roxy API、Roxy 环境生命周期 |
| `config/cloakbrowser.py` | CloakBrowser 无头/humanize/geoip/语言时区/指纹 seed |
| `config/codex.py` | Codex OAuth、授权驱动、CPA 管理接口、接码平台 |
| `config/email.py` | 邮箱来源、OTP 轮询、QQ IMAP、域名邮箱、Cloudflare Worker 临时邮箱 |
| `config/proxy.py` | 代理池 |
| `config/register.py` | 默认邮箱、密码、显示名 |
| `config/twofa.py` | 2FA 开关 |
| `config/humanize.py` | 随机停顿/人工节奏 |
| `config/flow_trigger.py` | 注册成功后触发 Flow |
| `config/browser.py` | 协议模式浏览器指纹 |
| `config/openai_protocol.py` | OpenAI OAuth/Sentinel 参数 |

WebUI 配置页保存后会调用热加载；Roxy、Codex、邮箱、代理、人工节奏等常用项可立即生效。

---

## 数据与产物

| 路径 | 内容 |
|---|---|
| `用于注册的邮箱.txt/json` | Outlook 邮箱池及状态 |
| `用于注册的API邮箱.txt/json` | 通用 API 邮箱池及状态 |
| `注册成功的邮箱.txt/json` | 注册成功账号 |
| `注册成功的token.txt` | ChatGPT access token |
| `accounts/` | 每次运行的批次归档 |
| `codex_accounts/` | Codex OAuth 凭证 JSON |
| `注册任务.json` | WebUI 注册任务表 |
| `注册日志/` | 注册任务日志、Codex 补跑日志 |
| `accounts_viewer.html` | 本地账号查看页 |

批次目录示例：

```text
accounts/20260709-10个-3线程/
├── 注册成功的邮箱.txt
├── 注册成功的token.txt
├── 注册成功整行.txt
└── 注册成功账号.json
```

---

## 当前主流程

### Roxy 注册流程

```text
创建/打开 Roxy Profile
  ↓
打开 chatgpt.com/auth/login
  ↓
按 DOM 技术属性定位邮箱输入框，避免误点 Google/Apple/Microsoft
  ↓
提交邮箱表单
  ↓
如进入 create-account/password：设置密码并提交
  ↓
等待邮箱验证码页
  ↓
读取邮箱 OTP 并提交
  ↓
如进入 about-you/profile：填写姓名 + 年龄或生日
  ↓
进入 ChatGPT，读取 /api/auth/session accessToken
  ↓
可选 2FA
  ↓
可选 Codex OAuth
  ↓
保存账号与批次归档
  ↓
关闭/删除 Roxy Profile
```

### Codex Roxy 授权流程

```text
获取 Codex 授权地址（CPA 或 local PKCE）
  ↓
Roxy 打开授权页
  ↓
邮箱登录 + 邮箱 OTP
  ↓
手机号验证：取号 → 填号 → 发送 → 等短信 → 填 OTP
  ↓
等待 consent/workspace/callback
  ↓
提交 callback 给 CPA 或本地换 token
  ↓
保存 codex_accounts/codex-邮箱*.json
```

---

## 常见问题

### 配置保存后没生效？

WebUI 配置页保存后会热加载。Codex 补跑线程启动前也会重新热加载一次配置。

如果你直接手改 `config/*.py`，CLI 进程需要重启；WebUI 建议在配置页修改。

### Roxy 无头保存后仍弹窗口？

检查：

```python
ROXY_OPEN_HEADLESS = True
```

并确认 Roxy 版本支持 `/browser/open` 的 `headless` 参数。日志会打印实际传入的 `headless`。

### 出口 IP 不是日本时点到 Google 登录？

当前 Roxy 注册邮箱入口已改为只按 DOM 技术属性定位，并排除三方登录按钮。不会再靠按钮文字匹配“Continue”。

### Codex 显示 `Check your phone` 被误判失败？

已兼容：`Check your phone / Enter the verification code...` 会识别为手机验证码页，进入等待短信验证码流程。

### 手机 OTP 提交后日志曾显示失败，但后面成功？

已修复：提交手机 OTP 后会等待页面离开手机号流程或 callback，不再 3 秒后用旧页面文案误判失败。

### Codex 失败但注册成功怎么办？

账号会保存，Codex 状态会标记失败。可以在 WebUI 账号页点击补跑，或使用：

```bash
python tools/test_codex_oauth.py --email <邮箱> --verbose
```

### Cloudflare Worker 邮箱怎么配？

将 `EMAIL_SOURCE` 设为 `cloudflare`，并配置 `CLOUDFLARE_API_BASE` 等（见上文「Cloudflare Worker 临时邮箱」）。  
注意与 `cloudflare_domain`（QQ IMAP 转发）不是同一来源。

### 没有接码平台能注册吗？

可以。关闭：

```python
ENABLE_CODEX_AUTO = False
```

注册主流程不依赖接码，Codex 自动授权才需要。

---

## 项目结构

```text
.
├── main.py                         # CLI 入口
├── web.py                          # WebUI 入口
├── config/                         # 配置
│   ├── roxybrowser.py              # RoxyBrowser 注册/Codex 驱动
│   ├── cloakbrowser.py             # CloakBrowser 注册驱动配置
│   ├── browser_use.py              # Browser Use Cloud 配置
│   ├── codex.py                    # Codex OAuth / 授权驱动 / CPA / 接码
│   ├── email.py                    # 邮箱来源/OTP
│   ├── proxy.py                    # 代理池
│   ├── register.py                 # 默认注册信息
│   └── ...
├── core/
│   ├── roxy_registration.py        # Roxy / 浏览器注册页面流程
│   ├── cloakbrowser_registration.py # Cloak 注册入口
│   ├── cloakbrowser_driver.py      # Cloak Playwright→Selenium 风格适配层
│   ├── browser_use_registration.py # Browser Use + Playwright 注册流程
│   ├── browser_use_client.py       # Browser Use CDP 客户端
│   ├── roxy_codex_oauth.py         # Roxy / Cloak 浏览器 Codex OAuth 页面流程
│   ├── roxybrowser_client.py       # Roxy API 客户端
│   ├── registration_service.py     # WebUI 注册线程池
│   ├── codex_oauth.py              # Codex 协议/Roxy/Cloak 调度
│   ├── email_provider.py           # 邮箱来源调度
│   ├── cf_temp_mail_client.py      # Cloudflare Worker 临时邮箱
│   ├── sms_provider.py             # 接码平台
│   ├── account_export.py           # 保存账号/批次归档
│   └── db.py                       # 文件数据库
├── webui/
│   ├── app.py                      # Flask API
│   ├── config_editor.py            # 配置读写/热加载
│   └── templates/index.html        # 单页控制台
├── sentinel/
│   ├── sdk.js
│   └── sentinel-runner.js
├── tools/
│   └── test_codex_oauth.py         # Codex 单独补跑
└── L_API.md                        # 本地 L 接码接口说明
```

---

## 使用建议

- 日常批量使用 WebUI，不建议直接同时开多个 CLI 进程。
- 注册线程数建议不超过可用代理数。
- Roxy 一号一环境建议保持开启，降低环境污染。
- 调试页面问题时可临时设置：

```python
ROXY_KEEP_BROWSER_OPEN = True
ROXY_OPEN_HEADLESS = False
```

- 调试完再改回自动关闭/删除环境。

---

## 技术依赖

- [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) — Stealth Chromium / Playwright 自动化指纹浏览器支持
- [browser-use](https://github.com/browser-use/browser-use) — Browser Use Cloud / Playwright CDP 云端浏览器能力支持
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — Codex OAuth 凭证格式参考
- [curl_cffi](https://github.com/yifeikong/curl_cffi) — 底层 HTTP 库，提供 TLS 指纹 impersonate 能力

---

## License

MIT
