# Turb GPT Register

ChatGPT / OpenAI 账号自动注册与 Codex OAuth 授权工具。当前项目支持三套注册驱动：

- **protocol**：原纯协议注册，基于 `curl_cffi` + Sentinel/PoW。
- **roxy**：RoxyBrowser 指纹浏览器 + Selenium 自动化注册，兼容新版页面流，例如 `create-account/password`、`about-you` 年龄/生日表单、地区本地化页面等。
- **cloak**：CloakBrowser + Playwright 适配层自动化注册，支持免费 binary、无头模式、humanize、固定 fingerprint seed、代理 geoip。
- **browser_use**：Browser Use Cloud stealth Chromium + Playwright（可选住宅代理，无需本机安装 Roxy）。
- **skyvern**：Skyvern Browser Sessions 云端浏览器 + Playwright CDP。

项目提供 **CLI** 和 **本地 WebUI** 两种使用方式。日常推荐使用 WebUI。

> 项目说明：本仓库为 [myfanhua/turb-gpt-free-register](https://github.com/myfanhua/turb-gpt-free-register) 的个人 fork，基于其代码做二次开发（上游本身基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 改造）。

- TG 交流群：[https://t.me/+uC3Ix0l2E085Njhl](https://t.me/+uC3Ix0l2E085Njhl)

> 开源版说明：仓库只保留源码、配置模板和文档；运行时账号、Token、邮箱池、Codex 凭证、日志等真实数据均已通过 `.gitignore` 排除。

---

## 特别鸣谢

[![IPWO 住宅代理](https://raw.githubusercontent.com/myfanhua/turb-gpt-free-register/main/static/telegram-cloud-photo-size-5-6154589162401632962-y.jpg)](https://www.ipwo.net/?code=XEP358YGZ)

IPWO 住宅代理提供覆盖195+国家和地区的住宅 IP 资源，支持多地区网络环境配置，适用于 AI 应用、浏览器自动化、海外服务访问及数据采集等场景。
重点！2GB 动态住宅流量无门槛发放，[领取入口](https://www.ipwo.net/?code=XEP358YGZ)，进群不定时 IP 福利发放。

## 功能概览

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
- 通用 IMAP 邮箱池：每行 `邮箱----IMAP密码` 或 `邮箱:IMAP密码`，服务器、端口和 SSL 在导入界面统一配置
- GPTMail 临时邮箱 API：运行时随机生成邮箱并自动收取验证码
- Remail 开放 API：按项目下单短效邮箱并自动收取验证码（`remail`）
- `EMAIL_SOURCE` 支持多个来源组合，例如：

```python
EMAIL_SOURCE = "outlook,generic_api,imap"
```

- MailNest-迈巢：Outlook 临时邮箱

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
- Codex 凭证保存到 SQLite 的 `codex_accounts` 表。

### WebUI

- 批量启动注册任务。
- 实时查看任务日志。
- 动态调整注册线程数，提交后新任务立即使用最新值。
- 批量补跑 Codex，补跑线程数每次提交即时生效。
- 管理账号、邮箱池、Codex 凭证；账号页支持单个/批量换绑邮箱并指定新邮箱来源，换绑后同时展示原邮箱与当前邮箱，支持查看换绑日志，并自动查活刷新 AT。
- 邮箱池导入默认不创建账号；勾选“导入后默认视为注册成功账号”后，会将邮箱池标记为已用并同步显示在账号页，可直接批量补跑 Codex。
- Roxy/Cloak 浏览器注册完成后统计整个浏览器会话的上传、下载和总流量，任务列表与账号扩展信息均会保存结果；Browser Use/Skyvern 云端浏览器不启用本地流量监听、资源拦截或 JS 覆盖率采集。
- 配置页支持热加载，保存后无需重启。
- Roxy 团队/项目可在配置页获取并保存。

### 数据存储

- 账号、邮箱库、任务及 Codex 凭证运行时统一存储在项目根目录 `turb.sqlite3`，按业务拆分为 `accounts`、`email_pool`（邮箱库）、`registration_jobs`、`codex_accounts` 和 `codex_agent_accounts` 五张表。
- 数据库启用 WAL、超时等待和常用字段索引，WebUI 的账号、套餐状态、邮箱库、Codex 和任务分页直接执行 SQLite `COUNT(*) + LIMIT/OFFSET`，不再先读取全量数据后由 Python 切片。
- 首次启动会自动把现有 JSON/历史 SQLite 数据迁移到新数据库；迁移完成后不再读写账号、任务、邮箱池和 Codex 凭证 JSON/TXT 文件。
- `turb.sqlite3*` 属于运行时数据，已加入 `.gitignore`，请纳入备份策略。

### 浏览器网络流量统计

浏览器驱动会从打开注册页开始统计，到注册后停留结束、浏览器关闭前完成汇总。结果包含：

- 上传字节、下载字节、总字节数；
- HTTP 请求数、失败/未完成请求数；
- WebSocket 帧 payload 字节（如流程使用 WebSocket）。

现代/Legacy WebUI 的注册任务列表会显示总流量，完整结构保存在任务记录的 `network_traffic` 和成功账号的 `extra_json` 中。统计为浏览器侧可观测的请求/响应流量，不包含 TLS/IP/代理隧道额外开销，也不包含邮箱 API、Roxy API 或 CDP 控制通道流量。

#### 省流量模式

在 WebUI「浏览器画像」中开启「本地浏览器省流量模式」，或在 `.env` 设置（仅 Roxy/Cloak 生效）：

```dotenv
BROWSER_DATA_SAVER_MODE=True
BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES=["image", "media"]
# URL glob 列表；WebUI 中则是一行一条
BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS='["**://auth.openai.com/awe/api/v2/rum**", "**://chatgpt.com/awe/api/v2/rum**", "**://chatgpt.com/ces/statsc/flush**", "**://connect.facebook.net/**", "**://analytics.tiktok.com/**", "**://snap.licdn.com/**", "**://bat.bing.com/**", "**://accounts.google.com/gsi/client**"]'
```

Roxy/Selenium 会在启动参数中关闭图片加载，并使用 Chrome CDP 拦截常见图片、媒体等 URL 后缀及配置的 URL glob（因此也能覆盖无扩展名资源）；Cloak 使用 Playwright 按资源类型和 URL glob 拦截。Browser Use/Skyvern 是云端浏览器，不安装本地省流量拦截器，始终保留完整页面资源。默认只拦截 `image`、`media`，以及配置中列出的 RUM/广告统计 URL，不会按类型拦截登录所需的核心脚本、接口和 WebSocket。Playwright 会放行带验证码/challenge 关键词的 URL；Roxy 的 Chromium 图片开关和 CDP URL 黑名单无法提供 URL 例外规则，若页面出现验证码或布局异常，关闭该模式后重试。

Job 208 的明细显示，当前规则实际拦截了 5 个第三方脚本（Google GSI、Facebook、TikTok、LinkedIn、Bing）以及 380 次 RUM 请求；邮箱/密码注册成功。注册侧下载约 9.92 MiB，其中脚本约 8.90 MiB，主要来自 ChatGPT 核心 CDN chunk。

当前默认规则只包含 RUM/广告统计和 Google GSI。邮箱/密码注册不使用 Google 登录，因此可以保留 GSI 规则；如果将来启用 Google 登录，需从「省流量 URL 屏蔽规则」中移除以下行：

```text
**://accounts.google.com/gsi/client**
```

疑似 CES 遥测的 `**://chatgpt.com/ces/v1/rgstr` 约 277 KiB/轮，也可在单独验证注册成功率后加入。

不要屏蔽 `chatgpt.com/cdn/assets/*.js`、`auth-cdn.oaistatic.com/assets/*.js`、`sentinel.openai.com`、`chatgpt.com/backend-api/sentinel/*`、`ab.chatgpt.com/v1/initialize`、`chatgpt.com/realtime/wm` 和注册/OTP/session API。Job 207 屏蔽 `7aaae702-*.js` 后出现 OTP 输入框缺失；Job 208 放行该 chunk 后完整成功，因此不能仅凭低函数执行比例屏蔽 CDN chunk。URL 规则填写 `[]` 可恢复为仅按资源类型拦截，空白则使用内置默认规则。

如需拦截 CSS，可加入 `stylesheet`：

```dotenv
BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES=["image", "media", "stylesheet"]
```

CSS 通常不是注册接口必需，但会影响隐藏元素、布局和可见性判断，建议先单独测试；出现元素找不到或点击异常时移除 `stylesheet`。

#### 资源明细日志

需要分析注册流程中哪些资源占流量时，开启：

```dotenv
BROWSER_TRAFFIC_DETAIL_LOG=True
BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES=2000
```

启用统计的 Roxy/Cloak 注册任务结束后，日志会输出 `[资源明细]` 行，包含资源 URL、类型、HTTP 方法、状态码、上传大小、下载大小、响应 body/header 大小，以及 `failed`、`blocked`、`unfinished`、`cache` 状态；明细按单请求总字节从大到小排列。Browser Use/Skyvern 云端浏览器不启用该监听。`ws_upload/ws_download` 表示 WebSocket 帧 payload。Playwright 缓存状态在无法从 API 确认时显示 `unknown`，Selenium/CDP 能识别时显示 `hit` 或 `miss`。URL 查询参数值、data/blob URL 内容不会写入日志。

把一轮注册的 `[资源明细]` 日志发回后，可以按域名、路径、资源类型和实际字节量判断下一步是否适合继续拦截；`stylesheet`、`font` 等类型需要结合注册是否受影响再启用。

#### JS 执行函数覆盖率

需要确认某个 CDN chunk 是否在 Roxy/Cloak 注册流程中真正执行时，开启：

```dotenv
BROWSER_JS_COVERAGE_LOG=True
BROWSER_JS_COVERAGE_MAX_ENTRIES=1000
```

Roxy/Selenium 会在当前 Chrome target 上启用 CDP `Profiler.startPreciseCoverage`，Cloak 会为已发现的 Chromium Page 建立 CDP session；Browser Use/Skyvern 不启用 JS 覆盖率监听。任务结束时日志包含：`[JS执行汇总]`、每个脚本的 `[JS脚本]`、实际执行函数的 `[JS执行]`（函数名、调用次数、`startOffset-endOffset:count`）以及“本次未观察到执行范围”的 `[JS候选]`。`network_traffic.js_coverage` 会保存脚本级摘要和候选 URL，逐函数 offset 只写日志，不保存源码、参数或返回值。

`[JS候选]` 仅表示该轮覆盖率没有观察到执行代码，不能单独证明可以屏蔽：先用一轮未屏蔽核心脚本的成功注册作为基线，再一次只屏蔽一个候选并对比 OTP、session、资料页和成功率。脚本若来自 `data:`/`blob:`/扩展页不会列为 URL 屏蔽候选；跨 popup/多 target 的 Selenium 页面只覆盖当前 CDP target。若 CDP Profiler 不受当前指纹浏览器支持，日志会标记 `supported=False`，不会影响注册流程。

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
# 推荐使用项目虚拟环境，不要直接使用系统 pip
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
node --version
```

启动 WebUI 时，`webui.sh` 会优先使用 `.venv/bin/python`：

```bash
./webui.sh start
```

如果 macOS 升级或卸载了创建虚拟环境时使用的 Python，旧 `.venv` 中的 Python 软链接可能失效。此时请使用当前已安装的 Python 重新创建环境：

```bash
rm -rf .venv
python3.12 -m venv .venv   # 也可以替换为当前已安装的 Python 3.10+
.venv/bin/python -m pip install -r requirements.txt
./webui.sh start
```

验证当前 WebUI 使用的解释器和 Flask：

```bash
.venv/bin/python -c 'import sys, flask; print(sys.executable); print(flask.__version__)'
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
- `REMAIL_API_KEY`（`EMAIL_SOURCE=remail` 时）
- `CPA_MANAGEMENT_KEY`
- `SMS_API_KEY`
- `L_ADMIN_AUTH_CODE`
- `H_ADMIN_AUTH_CODE`

WebUI 配置页保存这些字段时会写入 `.env`（不是 config 源码）。

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

#### 通用 IMAP 邮箱

在 WebUI「邮箱池 → 导入」选择“通用 IMAP 取码邮箱”，每行格式：

```text
email----imap_password
email:imap_password
```

- 在导入类型选择“通用 IMAP 取码邮箱”后，填写统一的 IMAP 服务器、端口和 SSL 配置。
- IMAP 登录用户名固定使用对应邮箱地址，无需额外配置。
- 端口通常为 `993`，SSL 默认启用。
- 示例：`user@example.com----app-password`
- 默认读取 `INBOX`，可在「配置 → 邮箱 / OTP → 通用 IMAP」修改。

将邮箱来源设置为：

```python
EMAIL_SOURCE = "imap"
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

#### Remail 开放 API

Remail API 文档：[https://remail.aishop6.com/docs](https://remail.aishop6.com/docs)。该服务使用 API Key
按项目创建短效接码订单，订单返回的邮箱和 service token 会自动用于后续取码。

在 WebUI「配置 → 邮箱 / OTP」填写：

- `REMAIL_API_KEY`：Remail 控制台生成的 `rk-` 开头 API Key；
- `REMAIL_PROJECT_ID`：Remail「项目」列表中用于 ChatGPT/OpenAI 验证码的 `projectId`；
- `REMAIL_EMAIL_SUFFIX`：下单后缀，微软邮箱通常填 `outlook.com`。

然后设置：

```dotenv
USE_EMAIL_SERVICE=True
EMAIL_SOURCE=remail
REMAIL_API_BASE=https://remail.aishop6.com
REMAIL_API_KEY=你的_Remail_API_Key
REMAIL_PROJECT_ID=项目ID
REMAIL_EMAIL_SUFFIX=outlook.com
REMAIL_SERVICE_MODE=purchase
REMAIL_SUPPLY_POLICY=public_only
```

`REMAIL_SERVICE_MODE` 默认为 `purchase`（长效购买，可重复收件），也可改为 `code`（短效接码）。
`REMAIL_SUPPLY_POLICY` 默认为 `public_only`，也可改为 `private_first`。每个注册任务会创建一个
对应模式的订单，验证码通过 `/v1/pickup` 获取；Remail 订单余额和对应项目库存需可用。

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

协议注册会使用 `curl_cffi`、Sentinel/PoW、代理池等配置，并强制走
`create-account/password` 邮箱密码注册流程。注册密码与浏览器驱动一致：
优先使用 `config/register.py` 的 `REGISTER_PASSWORD`，未配置时随机生成，
并保存到账号的 `extra_json.registration_password`。

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

推荐使用项目根目录单脚本后台管理：

```bash
./webui.sh start      # 启动
./webui.sh stop       # 关闭
./webui.sh restart    # 重启
./webui.sh status     # 状态
./webui.sh logs       # 查看实时日志
```

脚本默认启动 `http://127.0.0.1:5000`，日志写入 `logs/webui.log`，PID 写入 `run/webui.pid`。

可通过环境变量调整：

```bash
PORT=8000 OPEN_BROWSER=1 ./webui.sh start
HOST=0.0.0.0 PORT=5000 ./webui.sh restart
AUTH_CODE=你的授权码 ./webui.sh start
```

也可以直接前台启动：

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
| Codex 授权 | 查看/下载/删除 SQLite 中的 Codex 凭证 |
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
- SQLite `accounts.payload` 中的 `extra_json.registration_password`

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
| `turb.sqlite3` | 账号、邮箱库、任务、Codex 和 Agent 凭证全部数据 |
| 旧 JSON/TXT/Codex 文件 | 仅用于首次迁移，运行期间不再读写 |
| `注册日志/` | 注册任务日志、Codex 补跑日志 |

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
保存账号到 SQLite
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
保存 Codex 凭证到 SQLite
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
│   ├── browser_data_saver.py       # Roxy/Cloak 本地浏览器省流量资源拦截
│   ├── browser_traffic.py          # 浏览器注册 HTTP/WebSocket 流量统计
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
│   ├── account_export.py           # 注册后处理与 SQLite 保存
│   └── db.py                       # SQLite 数据库与一次性迁移
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

## 🙏 致谢

- [LINUX DO](https://linux.do) — 社区交流与用户反馈
- [RoxyBrowser](https://roxybrowser.cn/invite/NvH4Jx) — 免费提供 5 个窗口
- [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) — Stealth Chromium / Playwright 自动化指纹浏览器支持
- [browser-use](https://github.com/browser-use/browser-use) — Browser Use Cloud / Playwright CDP 云端浏览器能力支持
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — Codex OAuth 凭证格式参考
- [curl_cffi](https://github.com/yifeikong/curl_cffi) — 底层 HTTP 库，提供 TLS 指纹 impersonate 能力

---

## License

MIT
