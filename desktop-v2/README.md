# 注册工作台 V2

Electron + TypeScript + Playwright 桌面版。主流程为：账号导入、注册队列、邮箱取码、浏览器注册、Session 保存和注册后账户质检。

- Outlook OAuth 邮箱账号导入与验证码读取
- HTTP / SOCKS5 代理和注册并发队列
- 本地代理与动态代理可组成 `本地 -> 动态 -> 目标` 链路；动态代理按账号独占并在取用后移出代理池
- Edge 可视化注册流程；人机验证由用户在浏览器中完成
- 使用 ChatGPT CSRF 授权入口执行注册与登录
- 已设置 GPT 密码和 Authenticator MFA 的账号可自动登录：填写密码、生成并提交 TOTP 验证码
- 登录或注册完成后保存 Session JSON、Access Token、Storage State、过期时间与更新时间
- Session 有效性独立检测；HTTP 401 只标记 Session 失效，不直接判定封号
- 免费试用资格检测
- 登录错误与 OpenAI 邮件双通道封号/停用判断
- Outlook 收件箱最近 20 封邮件查看
- 一键迁移旧版 `state.json` 中的账号、密码、2FA、Session、试用状态和代理设置

## 账号格式

```text
email----邮箱密码----client_id----refresh_token
```

已经设置 GPT 密码和 MFA 的账号可追加：

```text
----gpt_password=GPT登录密码----2fa=Authenticator密钥
```

选中账号后使用 `登录获取 Session`。程序会根据当前页面自动区分邮箱验证码和 Authenticator 验证码。

## 启动

```powershell
cd D:\openai-register-paylink-ui\desktop-v2
npm install
npm start
```

数据保存在 Electron 用户数据目录的 `registration-state.json`，不再写入旧版 59 MB 的 `state.json`。该文件包含邮箱 OAuth 凭据、GPT 密码、2FA 密钥和 Session，属于敏感文件，请勿发送或提交到公开仓库。
