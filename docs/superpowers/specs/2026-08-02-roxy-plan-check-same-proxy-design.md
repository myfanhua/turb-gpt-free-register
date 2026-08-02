# Roxy 注册后套餐复检复用同一代理设计

## 背景

Roxy 注册环境会从代理池为每个账号生成独立 `sid`，并通过本地代理链使用韩国住宅出口完成注册。注册成功后，`save_account_data()` 会异步触发套餐查询，但当前没有把 Roxy 本轮实际代理传给查询队列。因此首次套餐检测和两秒后的 Plus 资格复检会回退到全局 `PLAN_CHECK_PROXY`，造成同一新账号在短时间内从韩国注册出口切换到美国检测出口。

现有两秒复检用于等待新账号的 Plus 试用资格同步，应继续保留；本次只修复代理上下文丢失，不增加延迟检测，也不延长注册任务。

## 方案选择

### 方案一：关闭注册后套餐查询

避免跨地区请求，但会失去自动套餐和 Plus 资格结果，不符合当前使用方式。

### 方案二：让全部套餐查询固定走韩国代理池

能保持国家一致，但未必复用注册时的同一 `sid` 和出口 IP，且会改变旧账号手动检测的全局策略。

### 方案三：注册后自动查询复用本轮 Roxy 代理（采用）

Roxy 创建环境时保留本轮实际代理 URL。注册成功保存账号时，将该代理只传给 `registration_auto` 套餐查询。首次检测和两秒复检继续使用 `plan_check_service` 已有的同一个 `proxy` 参数，因此复用注册时的 `sid` 和韩国出口。手动检测仍使用全局 `PLAN_CHECK_PROXY`。

## 数据流

```text
pick_proxy() 生成账号专属 sid
  -> prepare_proxy_for_roxy() 生成 Roxy 可用代理
  -> RoxyOpenResult 保存 registration_proxy
  -> run_roxy_registration() 完成注册并提取 accessToken
  -> save_account_data(proxy_used=registration_proxy)
  -> enqueue_account_plan_check(proxy=registration_proxy)
  -> 首次套餐检测
  -> 等待 PLAN_CHECK_REGISTRATION_RECHECK_DELAY=2.0
  -> 使用同一个 registration_proxy 复检 Plus 资格
  -> 关闭并删除 Roxy Profile
```

## 组件修改

### `core/roxybrowser_client.py`

- 为 `RoxyOpenResult` 增加可选的 `registration_proxy` 字段。
- `create_profile()` 或 `open_profile()` 保存本轮实际传给 Roxy 的代理 URL。
- 不在日志或 API 响应中输出代理账号密码。

### `core/roxy_registration.py`

- 保存账号时把 `opened.registration_proxy` 作为 `proxy_used` 传入。
- 不改变 Token 提取、Codex、Roxy Profile 清理顺序。

### `core/account_export.py`

- 注册后自动套餐查询调用 `enqueue_account_plan_check()` 时传入 `proxy=proxy_used`。
- 空代理保持现有行为，继续由套餐查询模块解析全局网络策略。

### `core/plan_check_service.py`

- 保持现有首次查询及两秒复检逻辑。
- 两次请求继续使用相同的显式 `proxy` 参数。

## 错误处理

- Roxy 未配置代理时，`registration_proxy` 为空，套餐查询按原有全局配置运行。
- 显式注册代理连接失败时，套餐查询记录真实网络错误，不自动切换到不同国家出口。
- 套餐查询失败不影响注册结果和 Token 保存。
- 所有日志只显示脱敏代理摘要。

## 测试

增加测试覆盖：

1. Roxy 创建环境后，`RoxyOpenResult.registration_proxy` 保存本轮实际代理。
2. 账号保存后，自动套餐查询收到相同代理。
3. 首次查询与两秒复检使用完全相同的代理参数。
4. Roxy 未使用代理时保持现有全局套餐查询行为。
5. 手动套餐检测仍使用全局 `PLAN_CHECK_PROXY`，不受注册代理影响。

## 验收标准

- 每个账号仍生成独立 `sid`。
- 注册、首次套餐检测和两秒复检使用相同代理 URL。
- `PLAN_CHECK_REGISTRATION_RECHECK_DELAY` 保持 `2.0`。
- 注册耗时不增加新的等待步骤。
- Roxy Profile 仍在任务结束时关闭并删除。
- 现有测试全部通过，并新增代理传递回归测试。
