# Default-all-domains-1784468371563.json 纯协议链路与指纹补齐说明

来源文件：`Default-all-domains-1784468371563.json`，Reqorder HAR，抓包时间窗口：2026-07-19 21:36:16 ~ 21:37:30（Asia/Shanghai）。

## 1. 总览

- 总请求：236
- 域名分布：`chatgpt.com` 125、`browser-intake-datadoghq.com` 107、`auth.openai.com` 2、`ab.chatgpt.com` 1、测试回调 1
- 主要状态：200 共 112、202 共 111、0 共 13（Datadog/octet-stream 中断或被采集器标记）
- 主浏览器画像：macOS + Chrome 149；语言/时区由代理出口 IP 自动决定，本 HAR 样本为 zh-CN + Asia/Shanghai
- ChatGPT 前端版本：`prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c`
- OAI build number：`8370486`
- Sentinel SDK：`20260219f9f6`

## 2. 关键链路顺序

### A. ChatGPT 匿名态首页/模型预热

1. `POST /ces/v1/rgstr`：Statsig 注册/flush，上报头包含 `oai-client-build-number`、`oai-client-version`、`oai-device-id`、`oai-language`、`oai-session-id`。
2. `GET /ces/v1/projects/oai/settings`
3. `GET /backend-anon/accounts/check/v4-2023-04-27?timezone_offset_min=-480`
4. `GET /backend-anon/me`
5. `POST /backend-anon/sentinel/chat-requirements/prepare`
6. `GET /backend-anon/system_hints?...`、`GET /backend-anon/models?...`
7. `POST /backend-anon/conversation/init`
8. `POST /backend-anon/sentinel/chat-requirements/finalize`

### B. NextAuth 发起 OpenAI OAuth

1. `GET /api/auth/providers`
2. `GET /api/auth/csrf`
3. `POST /api/auth/signin/openai?...screen_hint=login_or_signup&login_hint=<email>`
   - form：`callbackUrl=https://chatgpt.com/&csrfToken=<csrf>&json=true`
   - 响应返回 `auth.openai.com/api/accounts/authorize?...` URL。

### C. Auth Web 邮箱 OTP / about-you / create_account

抓包中 `auth.openai.com` 业务接口只有两条：

1. `POST https://auth.openai.com/api/accounts/email-otp/validate`
   - body：`{"code":"<6位OTP>"}`
   - 本次抓包未带 `openai-sentinel-token`。
2. `POST https://auth.openai.com/api/accounts/create_account`
   - body：`{"name":"<name>","birthdate":"YYYY-MM-DD"}`
   - 带 `openai-sentinel-token` 和 `openai-sentinel-so-token`。
   - token 内 `flow=oauth_create_account`。

### D. 登录态 ChatGPT bootstrap

创建成功后回调到 ChatGPT，并进入登录态 bootstrap：

- `GET /backend-api/accounts/optimized/check`
- `GET /backend-api/user_granular_consent`
- `GET /backend-api/me`
- `GET /backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480`
- `GET /backend-api/settings/user`
- `POST /backend-api/sentinel/chat-requirements/prepare`
- `GET /backend-api/system_hints?...`、`GET /backend-api/models?...`
- `POST /backend-api/conversation/init`
- `POST /backend-api/sentinel/chat-requirements/finalize`
- `GET /backend-api/conversations?...`、`GET /backend-api/client/strings` 等 sidebar/home 初始化接口。

完整逐条请求已抽取到：`docs/protocol_har_summary.json`。

## 3. 指纹 p 数组字段补齐

抓包里的 `p` 解码后均为 25 项数组：

| 下标 | 含义 | 本次抓包样本 |
|---:|---|---|
| 0 | `screen.width + screen.height` | HAR 样本为 `2730`（`1680x1050`），代码中只作为候选画像之一 |
| 1 | `new Date().toString()` | `Sun Jul 19 2026 ... GMT+0800 (中国标准时间)` |
| 2 | `performance.memory.jsHeapSizeLimit` | `4395630592` |
| 3 | 初始/PoW attempt | 初始 `1`，create_account token 为 `5` |
| 4 | UA | `Mozilla/5.0 ... Chrome/149.0.0.0 Safari/537.36` |
| 5 | script src | 见下方 JS 入口 |
| 6 | build id / data-build | ChatGPT prepare 为 `prod-fb4a8a...`；Auth token 为 `null` |
| 7 | `navigator.language` | `zh-CN` |
| 8 | `navigator.languages.join(',')` | `zh-CN` |
| 9 | 随机/耗时 | `1.2`、`1.4`、`4` 等 |
| 10 | navigator 原型/属性样本 | `createAuctionNonce`、`clearOriginJoinedAdInterestGroups`、`login` |
| 11 | document key / React key | `_reactListening...` 或 `__reactContainer$...` |
| 12 | window key | `scrollX`、`locationbar`、`ondevicemotion` |
| 13 | `performance.now()` | 浮点毫秒 |
| 14 | Sentinel 内部 sid | UUID，和 `oai-device-id` 不同 |
| 15 | URL search params | 空字符串 |
| 16 | `navigator.hardwareConcurrency` | `6` |
| 17 | `performance.timeOrigin` | 毫秒时间戳 |
| 18-24 | window feature flags | 本次均为 `0`，但 Chrome runner 保留 `requestIdleCallback` 可控 |

## 4. JS 入口补齐

HAR 没有直接保存 `.js` 响应正文，但从 Sentinel `p[5]` 还原出被采样到的 JS 入口：

1. `https://accounts.google.com/gsi/client`
2. `https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload`
3. `https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js`

项目本地已有 SDK 文件：`sentinel/sdk.js`；Node VM 执行器为 `sentinel/sentinel-runner.js`。本轮已按 HAR 补齐 runner 的 Chrome 149 DOM/Navigator/Window 样本。

## 5. 已同步到代码的纯协议细节

- `config/openai_protocol.py`
  - 更新 `OPENAI_BUILD_ID=prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c`
  - 新增 `OAI_CLIENT_BUILD_NUMBER=8370486`、`OAI_CLIENT_VERSION`
  - 补齐 Statsig/AB SDK key/version 常量。
- `config/browser.py`
  - 切到 Chrome 149 HTTP/JS 画像：UA、Client Hints、动态语言/时区；窗口/屏幕尺寸从画像池随机选择，HAR 的 `1680x1050` / `hardwareConcurrency=6` / `jsHeapSizeLimit=4395630592` 只作为候选之一。
  - 补齐 `createAuctionNonce`、`clearOriginJoinedAdInterestGroups`、`login`、`locationbar`、`scrollX`、`ondevicemotion` 等 HAR 出现的采样键。
- `core/session.py`
  - 所有前端 API 请求统一补 `oai-client-build-number`、`oai-client-version`、`oai-session-id`。
  - 会话内新增稳定 `react_container_key`，与 `react_listening_key` 一起供 p[11] 抽样。
- `core/sentinel.py`
  - p[11] 候选补齐 `__reactContainer$...`。
- `sentinel/sentinel-runner.js`
  - Chrome Navigator/DOM/Window 样本补齐。
  - Auth Sentinel token 支持 `data-build=null` 形态。
  - 默认 SDK src 对齐 `https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js`。

## 6. 重新生成摘要

```bash
./tools/analyze_har_protocol.py Default-all-domains-1784468371563.json -o docs/protocol_har_summary.json
```

## 7. `.env` 覆盖项

语言/时区不固定，按代理出口 IP 自动设置：

```env
AUTO_BROWSER_LOCALE_FROM_IP="True"
```

`BROWSER_LOCALE_PROFILE="jp"` 仅作为 GeoIP 检测失败时的兜底画像；正常情况下会根据出口国家/时区生成 `Accept-Language`、`navigator.language`、`navigator.languages`、`Date`/`Intl` 时区。

## 8. runner 继续补齐项

`sentinel/sentinel-runner.js` 不只补 HAR 中直接命中的几个 key，还需要保证 Python 画像传入 Node VM 后一致：

- `--language` / `--languages`：由 `BrowserSession.browser_profile` 传入。
- `--time-zone` / `--timezone-name` / `--timezone-offset-minutes`：由代理 GeoIP 画像传入。
- `process.env.TZ`：runner 启动时按 `--time-zone` 设置，使 VM 里的 `Date.toString()` 与代理时区一致。
- `Intl.DateTimeFormat().resolvedOptions().timeZone`：在 VM 内覆盖为同一个 `timeZone`。
- `document` React key：同时支持 `_reactListening...` 和 `__reactContainer$...`。
- `navigator` Chrome 专有采样：`createAuctionNonce`、`clearOriginJoinedAdInterestGroups`、`canLoadAdAuctionFencedFrame`、`login` 等。
- `window` Chrome/页面采样：`locationbar`、`scrollX`、`scrollY`、`ondevicemotion` 等。

因此语言/时区最终链路是：代理出口 IP → `pick_browser_profile()` → Python Sentinel `p` → runner CLI 参数 → Node VM `Date`/`Intl`/`navigator`。

## 9. 二次对齐检查与补齐

根据本文件再次对照代码后，继续补齐了这些容易漏掉的协议细节：

1. **Auth Web JSON 接口头部**
   - HAR 中 `auth.openai.com/api/accounts/email-otp/validate`、`create_account` 没有 `oai-client-*` 头。
   - 已将 `core.session.BrowserSession.get_auth_headers()` 改为 Auth RUM 形态：
     - `traceparent`
     - `tracestate`
     - `x-access-flow-invocation-id`
     - `x-datadog-*`
   - `chatgpt.com` 前端接口仍保留 `oai-client-*` / `oai-session-id`。

2. **NextAuth signin callbackUrl**
   - HAR form body 为 `callbackUrl=https://chatgpt.com/`。
   - 已将 `core.chatgpt_auth.signin_openai()` 从 `/login` 对齐为根路径 `/`。

3. **email-otp/validate Sentinel 策略**
   - HAR 样本中 `email-otp/validate` 未携带 `openai-sentinel-token`。
   - 已新增开关：`config.openai_protocol.SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE = False`。
   - 默认按 HAR 不带 Sentinel；如后续服务端要求，可改为 `True` 回退旧逻辑。

4. **timezone_offset_min 符号**
   - ChatGPT `accounts/check` URL 使用 JS `Date.getTimezoneOffset()` 语义：东八区是 `-480`，日本是 `-540`。
   - 已新增 `BrowserSession.js_timezone_offset_min()`，避免把内部 `timezone_offset_minutes`（东区为正）直接用于 URL。

5. **兜底地区**
   - 代理 GeoIP 检测失败时兜底日本：`BROWSER_LOCALE_PROFILE="jp"`。
   - GeoIP 成功时仍按出口 IP 自动设置语言/时区。

### 当前仍未强制补的链路

- `browser-intake-datadoghq.com` 的高频 RUM 批量上报：对注册业务不是必需链路，且会显著增加请求噪音，暂不强制模拟。
- `chatgpt.com/backend-anon/sentinel/chat-requirements/prepare/finalize` 与登录态 `backend-api/sentinel/...`：文档已记录其顺序；当前注册核心仍使用 `sentinel.openai.com/backend-api/sentinel/req` 生成 create_account 所需头。若后续要做到“首页 bootstrap 全量同形态”，可单独加一个可开关的 `chatgpt_bootstrap()` 预热模块。


## 10. 窗口/屏幕尺寸策略

窗口/屏幕尺寸不应全局固定为 HAR 的单个样本。当前策略：

- 同一个 `BrowserSession` 内：尺寸、DPR、CPU、JS heap 保持稳定，并同步传给 Python Sentinel `p` 和 Node runner。
- 不同 `BrowserSession`：从 `BROWSER_PROFILE_POOL` 随机挑选，形成自然分散。
- HAR 样本 `1680x1050 / hardwareConcurrency=6 / jsHeapSizeLimit=4395630592` 保留在池中，但不强制使用。
- 如果要复现实验抓包，可显式调用 `build_browser_environment(..., HAR_CAPTURE_BASE_PROFILE)`。

## 11. 第三轮细节优化

再次对齐 HAR 后，补齐/收敛了以下细节：

1. **`navigator.languages` 收敛**
   - HAR 解码的 `p[8]` 为单值 `zh-CN`，不是 `zh-CN,zh,en-US,en`。
   - 当前各地区画像的 `navigator.languages` 默认收敛为主语言单值，例如：
     - JP：`["ja-JP"]`
     - CN：`["zh-CN"]`
     - US：`["en-US"]`
   - `Accept-Language` 仍保留浏览器请求头常见的 q 权重链；二者不再强行完全相同。

2. **`requestIdleCallback` 默认关闭**
   - HAR 中 p[24] 为 `0`。
   - 已将 `WINDOW_FEATURE_FLAGS["requestIdleCallback"]` 默认改为 `0`，并从 `WINDOW_KEY_SAMPLES` 中移除，避免“p[24]=0 但 window key 抽到 requestIdleCallback”的自相矛盾。
   - runner 仍保留 `--request-idle-callback` 参数，必要时可显式开启。

3. **runner 独立运行兜底地区改为日本**
   - Python 正常调用 runner 时会传入代理 GeoIP 画像。
   - 如果手动直接运行 `sentinel-runner.js` 且未传语言/时区，默认兜底已改为：
     - `ja-JP`
     - `Asia/Tokyo`
     - `Japan Standard Time`
     - offset `540`

4. **NextAuth `/api/auth/*` header 单独拆分**
   - HAR 中 `/api/auth/providers`、`/api/auth/csrf`、`/api/auth/signin/openai` 未携带 `oai-client-*`。
   - 已新增 `BrowserSession.get_nextauth_headers()`，用于 NextAuth 和 `/api/auth/session`。
   - `get_chatgpt_headers()` 继续用于 `backend-api` / `backend-anon` / ChatGPT 前端 API，保留 `oai-client-*`。

## 12. 合规稳定性优化（降低误伤，不做规避）

本轮检查把优化边界限定为“减少异常状态下的误请求/重复请求/状态不一致”，不做绕过风控的指纹伪装增强。

### 12.1 会话级熔断器

已在 `BrowserSession` 增加会话级熔断：

- 命中 HTTP `429`：按 `Retry-After` 或默认 300 秒冷却。
- 命中 HTTP `403`：默认 900 秒冷却。
- 冷却期间当前 `BrowserSession` 的后续请求直接停止，避免在异常状态下继续打接口。
- 冷却上限为 3600 秒。

实现位置：

- `core/session.py`
  - `_raise_if_circuit_open()`
  - `_parse_retry_after()`
  - `_observe_response_for_circuit_breaker()`
  - `get()` / `post()` 自动接入

### 12.2 当前推荐策略

- 触发 403/429 后停止当前账号/代理会话，不在同一会话内连续重试。
- OTP 错误只按现有有限次数重发，不无限轮询。
- 保持代理 GeoIP、语言、时区、`Accept-Language`、`navigator.language`、`Date/Intl` 在同一会话内一致。
- 不强制模拟高频 Datadog RUM，避免引入大量非业务请求噪音。
- 不固定单一窗口尺寸；同一会话稳定，不同会话从合理桌面画像池随机。
