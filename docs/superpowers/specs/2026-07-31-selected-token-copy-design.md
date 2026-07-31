# 账号 Token 按选择复制设计

## 问题

账号页顶部“复制Token”按钮始终读取当前页全部账号 ID，因此即使用户只勾选一个邮箱，也会复制当前页所有 Token。

## 目标行为

- 存在勾选账号时，只请求并复制勾选账号的 `access_token`。
- 没有勾选账号时，保留原行为：复制当前页所有带 Token 的账号。
- 复制完成后提示实际复制的 Token 数量。
- 不改变每行“复制Token”按钮及后端敏感字段按需读取接口。

## 实现

调整 `webui/templates/index.html` 中 `copyAllTokens` 的点击处理：

1. 先读取 `ACCOUNT_SELECTED`。
2. 有选择时，以选择集作为账号 ID 来源。
3. 无选择时，以当前页 `ACCOUNTS` 中带 Token 的账号作为来源。
4. 继续通过 `/api/accounts/secret-bulk` 按需读取完整 Token。
5. 根据返回值显示复制数量；选择项没有 Token 时给出明确提示。

## 测试

增加模板回归测试，确认复制处理器优先使用 `ACCOUNT_SELECTED`，并保留无选择时的当前页回退逻辑。
