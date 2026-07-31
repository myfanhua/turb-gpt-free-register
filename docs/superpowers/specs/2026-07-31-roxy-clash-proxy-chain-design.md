# RoxyBrowser + Clash 链式代理设计

**日期：** 2026-07-31  
**状态：** 已确认推荐方案，待实现

## 目标

修复 RoxyBrowser 直连账密代理时出现的 `ERR_CONNECTION_CLOSED`，让注册浏览器通过 Clash 出口连接上游韩国账密代理，同时保持“每个注册账号一个 SID、下个账号更换 SID”。

## 根因

RoxyBrowser 当前把上游 `us.1024proxy.io:3000` 作为直接 HTTP 代理。该代理白名单要求从 Clash 出口访问；直连请求在 ChatGPT 登录页和静态资源阶段被关闭，因此页面停留在邮箱页。

## 架构

```text
RoxyBrowser profile
    │ local HTTP proxy + per-account local username
    ▼
Local chain bridge (127.0.0.1:PORT)
    │ CONNECT through Clash 127.0.0.1:7897
    ▼
Upstream HTTP proxy us.1024proxy.io:3000
    │ account credentials + SID generated for this profile
    ▼
ChatGPT / auth.openai.com
```

桥接服务监听本机 HTTP 代理端口。Roxy 创建环境时使用本地代理地址，代理用户名携带本轮 SID。桥接服务收到每个连接后，从本地 `Proxy-Authorization` 解析 SID，并替换为上游账密认证；所有上游 TCP 连接都先通过 Clash 建立，因此满足白名单要求。

## 组件与职责

1. **`tools/proxy_chain_bridge.py`**
   - 支持 HTTP 请求和 HTTPS CONNECT。
   - 通过 Clash HTTP 代理建立到上游代理的 CONNECT 隧道。
   - 解析本地代理用户名中的 SID；密码只作为本地占位，不写入日志。
   - 将上游账密和 SID 注入上游请求。
   - 为每个客户端连接独立转发，连接关闭后释放资源。

2. **`config/proxy.py` / `.env`**
   - 保留上游账密模板和 `{sid}` 轮换逻辑。
   - 新增链式桥接监听地址与 Clash 地址配置。
   - `pick_proxy()` 返回本地桥接代理 URL，同时把上游代理模板信息传给桥接服务。

3. **`core/roxybrowser_client.py`**
   - 创建 Roxy profile 前确保桥接服务已启动。
   - 将本轮 `pick_proxy()` 生成的 SID 通过本地代理认证传给桥接服务。
   - 失败时返回包含本地桥接端口、Clash 端口和上游连接阶段的诊断信息。

4. **生命周期管理**
   - WebUI 启动时启动一个本地桥接服务；若端口已监听则复用。
   - WebUI 退出时关闭由本进程创建的桥接服务。
   - Roxy profile 仍保持一号一环境，任务结束后关闭并删除 profile。

## 错误处理

- Clash `7897` 不可连接：桥接返回 `502`，错误明确标记 `CLASH_PREPROXY_UNAVAILABLE`。
- 上游代理认证失败：桥接返回 `502`，日志只记录主机、端口和请求阶段，不记录用户名密码或完整 URL。
- SID 缺失：拒绝该连接并提示需要由 Roxy profile 使用带认证的本地代理 URL。
- 上游连接成功但目标站点关闭：保留原始 HTTP 状态，便于区分目标站点故障与代理链故障。

## 验证标准

1. 单元测试覆盖：代理 URL 解析、SID 提取、Proxy-Authorization 替换、CONNECT 请求转发和敏感信息脱敏。
2. 集成检查：`127.0.0.1:BRIDGE_PORT` 监听；通过 Clash 连接上游代理成功；`ipinfo.io` 返回韩国出口。
3. Roxy smoke test：创建一个临时 profile，打开 ChatGPT 登录页，静态资源无 `ERR_CONNECTION_CLOSED`，邮箱提交后进入密码页或验证码页。
4. 轮换检查：连续创建两个 profile，桥接日志中 SID 不同，两个 profile 的出口查询结果分别成功。

## 不在本次范围

- 不修改邮箱池、验证码提取或 ChatGPT 页面选择器。
- 不保存原始代理密码到 Git 跟踪文件。
- 不改变 Roxy 的一号一环境和任务清理策略。
