# L 取号 API 文档

只包含接入端需要的两个接口：获取号码、获取验证码。`service` 和 `country` 由接入端自行配置。

## 基础信息

- API 基础地址：`http://localhost:8788`
- 请求和响应格式：`application/json`
- 后台接口需要授权：

```http
Authorization: Bearer <ADMIN_AUTH_CODE>
Content-Type: application/json
```

## 1. 获取号码

```http
POST /api/admin/l/take-phone
```

请求参数：

```json
{
  "service": "facebook",
  "country": "10",
  "maxPrice": "0.05"
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `service` | 是 | 项目代码，由接入端自行配置 |
| `country` | 是 | 国家 ID，由接入端自行配置 |
| `maxPrice` | 否 | 最高可接受价格 |

请求示例：

```sh
curl -s "http://localhost:8788/api/admin/l/take-phone" \
  -H "Authorization: Bearer <ADMIN_AUTH_CODE>" \
  -H "Content-Type: application/json" \
  -d '{"service":"facebook","country":"10","maxPrice":"0.05"}'
```

成功响应：

```json
{
  "item": {
    "id": "f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35",
    "activationId": "67908f935ab3410bd4c7f757",
    "service": "facebook",
    "country": "10",
    "countryName": "",
    "price": "",
    "phone": "9091234661",
    "status": "active",
    "lastCode": "",
    "createdAt": "2026-06-29T03:30:00.000Z"
  },
  "raw": "ACCESS_NUMBER:67908f935ab3410bd4c7f757:9091234661"
}
```

接入端需要保存：

| 字段 | 用途 |
| --- | --- |
| `item.id` | 调后台获取验证码接口使用 |
| `item.phone` | 获取到的手机号 |

常见错误：

```json
{"error":"请选择服务"}
{"error":"请选择国家"}
{"error":"取号失败：暂无号码","raw":"NO_NUMBERS"}
{"error":"取号失败：余额不足","raw":"NO_BALANCE"}
```

## 2. 获取验证码

```http
POST /api/admin/l/fetch-code
```

请求参数：

```json
{
  "id": "f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35"
}
```

请求示例：

```sh
curl -s "http://localhost:8788/api/admin/l/fetch-code" \
  -H "Authorization: Bearer <ADMIN_AUTH_CODE>" \
  -H "Content-Type: application/json" \
  -d '{"id":"f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35"}'
```

成功响应：

```json
{
  "item": {
    "id": "f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35",
    "phone": "9091234661",
    "status": "code_received",
    "lastCode": "899201"
  },
  "code": "899201",
  "message": "L 验证码获取成功",
  "raw": "STATUS_OK:899201",
  "fetchedAt": "2026-06-29T03:32:00.000Z"
}
```

暂未收到验证码：

```json
{
  "item": {
    "id": "f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35",
    "phone": "9091234661",
    "status": "active"
  },
  "code": "",
  "message": "等待验证码",
  "raw": "STATUS_WAIT_CODE",
  "fetchedAt": "2026-06-29T03:32:00.000Z"
}
```

常见错误：

```json
{"error":"缺少号码 ID"}
{"error":"号码不存在"}
{"error":"号码已释放，不能取验证码"}
```
## 3. 释放号码

```http
POST /api/admin/l/release
```

用于取消/释放已获取的 L 号码。释放后该号码状态会变为 `released`，不能再通过获取验证码接口取码。

请求参数支持释放单个号码：

```json
{
  "id": "f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35"
}
```

也支持批量释放号码：

```json
{
  "ids": [
    "f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35",
    "8d3c6f37-4d77-4c8c-b3df-2b0d6f4f9a11"
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 否 | 单个号码 ID，即获取号码接口返回的 `item.id` |
| `ids` | 否 | 批量号码 ID 数组；`id` 和 `ids` 至少传一个 |

请求示例：

```sh
curl -s "http://localhost:8788/api/admin/l/release" \
  -H "Authorization: Bearer <ADMIN_AUTH_CODE>" \
  -H "Content-Type: application/json" \
  -d '{"id":"f1b8b315-8c2a-4e23-8a94-fd1c2e4a9d35"}'
```

成功响应：

```json
{
  "updated": 1,
  "released": 1,
  "failed": []
}
```

部分失败响应示例：

```json
{
  "updated": 1,
  "released": 1,
  "failed": [
    {
      "id": "8d3c6f37-4d77-4c8c-b3df-2b0d6f4f9a11",
      "phone": "9091234662",
      "activationId": "67908f935ab3410bd4c7f758",
      "message": "订单不存在或已失效",
      "raw": "NO_ACTIVATION"
    }
  ]
}
```

响应字段说明：

| 字段 | 说明 |
| --- | --- |
| `updated` | 本次成功更新为已释放的数量；已释放号码重复释放也计入成功 |
| `released` | 与 `updated` 相同，兼容前端释放数量展示 |
| `failed` | 释放失败的号码列表；为空数组表示全部成功 |

常见错误：

```json
{"error":"请选择号码"}
{"error":"请先配置 LikeSim API Key"}
{"error":"未找到号码"}
```