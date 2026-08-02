# Roxy Codex Token 代理连续性设计

## 目标

Codex 本地 PKCE 流程从打开授权页到用 authorization code 换取 token，始终复用 Roxy 环境创建时实际使用的本地代理桥接 URL，避免最后一步重新从代理池抽取直连上游代理。

## 设计

- `RoxyOpenResult.registration_proxy` 是浏览器实际使用的代理入口，优先级高于调用方传入的代理。
- 本地 PKCE 模式创建一个专用 `BrowserSession`，通过上述代理先探测 `CODEX_TOKEN_URL` 的传输连通性。
- 预检放在邮箱登录和付费取号之前；代理 CONNECT 失败时立即结束本轮。
- 成功捕获 callback 后，复用同一个 `BrowserSession` 和代理入口执行 token exchange。
- 未创建临时 Roxy 环境时保留显式代理/现有代理池回退行为，并记录代理来源。

## 会话时长

当前代理用户名的 `-t-3` 表示三分钟粘性窗口。一次完整授权包含浏览器启动、邮箱 OTP、手机 OTP 和 callback，三分钟余量偏小。配置调整为 `-t-10`；每次任务仍生成新的 `sid`，所以不同账号继续更换出口会话。

## 验证范围

- 单元测试覆盖代理优先级、显式代理回退和 token 端点预检。
- 完整自动化测试验证无回归。
- 本轮不执行真实账号补跑，不领取 HeroSMS 号码。
