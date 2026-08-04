# 账号 Token 无短信修复设计

## 目标

修复账号套餐查询与 Codex 前置查询的网络出口漂移，确保默认复用账号注册时保存的 `proxy_used`，并明确失效 Access Token 的处理过程不自动进入 Codex OAuth、手机验证或 HeroSMS 取号流程。

## 根因

- 注册后的自动套餐查询已经传入注册代理，因此首次查询可成功。
- WebUI 单账号和批量套餐查询在请求体未提供 `proxy` 时传入 `None`，随后走独立默认网络策略，而不是账号保存的注册代理。
- Codex 补跑的 Plus 前置查询同样没有传入账号代理。
- 账号记录中的通用 `refresh_token` 属于 Outlook 邮箱取件凭据，不是 ChatGPT OAuth refresh token；不得用它刷新 ChatGPT Access Token。

## 设计

### 套餐查询代理选择

单账号查询：请求体显式包含 `proxy` 时沿用该值；未包含时使用账号的 `proxy_used`。

批量查询：请求体显式包含 `proxy` 时所有选中账号沿用该值；未包含时每个账号分别使用自身的 `proxy_used`，不得共享同一个默认代理变量。

### Codex 前置查询

Plus 前置查询复用账号的 `proxy_used`。这项修改只保证用户主动运行 Codex 补跑时前置查询网络一致，不会由 Token 失效或套餐查询失败自动启动 Codex 补跑。

### 无短信边界

- 套餐查询失败只记录查询结果，不触发 Codex 补跑。
- 失效 Access Token 不使用邮箱 `refresh_token` 做刷新。
- 本次修复不新增自动登录、手机验证或 HeroSMS 调用。
- 现有被服务端撤销的 Access Token 保持失效状态，后续如需重新授权必须由用户单独发起。

## 测试

- 单账号默认使用自身 `proxy_used`。
- 单账号显式代理覆盖账号代理。
- 批量查询逐账号使用不同的 `proxy_used`。
- 批量显式代理覆盖所有账号代理。
- Codex Plus 前置查询传入账号 `proxy_used`。
- 原有套餐查询、Codex gate 与全量测试继续通过。
