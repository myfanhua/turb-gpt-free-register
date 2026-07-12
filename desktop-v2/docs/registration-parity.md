# 注册与登录流程对照

新版将旧 `app.py` 中与账号注册相关的状态迁移到独立服务，支付、手机号池和提链逻辑不进入新版。

## 注册状态机

| 旧版行为 | 新版实现 |
| --- | --- |
| 访问 ChatGPT 首页并获取 CSRF / `oai-did` | `createOpenAiAuthUrl()` |
| 通过 `/api/auth/signin/openai` 创建 signup 跳转 | `createOpenAiAuthUrl(..., "signup")` |
| 路由错误最多重试 3 次 | `RegistrationService` |
| 人机验证可视模式等待 75 秒 | `RegistrationService` |
| 欢迎确认页在循环任意阶段处理 | `dismissWelcome()` |
| 邮箱输入与 Outlook OAuth 收码 | `fillEmailIfVisible()` / `waitForEmailCode()` |
| 单框或六个分段验证码 | `fillCodeAndSubmit()` |
| 注册密码页生成并保存 12 位密码 | `generatePassword()` / `fillPasswordAndSubmit()` |
| 姓名与年龄/出生日期，填写后等待 5 秒 | `fillAboutYouAndSubmit()` |
| 提交按钮加载时等待，最多 6 次重试 | `submitProfile()` |
| 完成后读取 `/api/auth/session` | `readSession()` |
| 保存 Access Token、Session JSON 与 Storage State | `sessionPatch()` / `StateStore` |

## 登录状态机

1. 使用 CSRF 登录授权入口并填写邮箱。
2. 识别密码页并使用保存的 `gptPassword`。
3. 页面提供多种 MFA 因素时优先选择 Authenticator app。
4. 根据 URL 和页面文案区分邮箱验证码与 TOTP。
5. TOTP 剩余时间不超过 8 秒时等待下一周期后提交。
6. 登录完成后保存新的 Session JSON、Access Token、Storage State 和过期时间。

## 代理与并发

- 动态代理数量少于所选账号数时拒绝启动。
- 每个账号独占一个动态代理，使用后从代理池移除。
- 仅本地代理时按配置并发，并以 1.5 秒间隔错峰启动。
- 未配置任何代理时保持单窗口。
- 同时配置本地与动态代理时使用 `本地代理 -> 动态代理 -> 目标站点` CONNECT/SOCKS5 链路。

## 状态判断

- HTTP 401 仅表示 Session 失效，不等同于封号。
- 只有接口明确返回 `account_deactivated`/deleted/deactivated，或 Outlook 邮件出现停用通知时，才标记为封号/停用。
- 免费试用接口按每批最多 20 个 Access Token 检测。
