# -*- coding: utf-8 -*-
"""
注册成功后自动跑 Codex OAuth 授权的配置项。
设置 ENABLE_CODEX = False 可完全跳过此步骤。

参数来源：CLIProxyAPI 源码 internal/auth/codex/openai_auth.go + pkce.go，
对照 https://github.com/router-for-me/CLIProxyAPI 逐行确认。
"""
from config.env_loader import env_str, apply_env_overrides


# 是否启用 Codex OAuth 授权（False = 跳过，不影响注册结果）
ENABLE_CODEX: bool = False

# Codex OAuth 客户端 ID（固定值，来自 CLIProxyAPI openai_auth.go:27 ClientID）
CODEX_CLIENT_ID: str = "app_EMoamEEZ73f0CkXaXp7hrann"

# 授权端点（openai_auth.go:25 AuthURL）
CODEX_AUTH_URL: str = "https://auth.openai.com/oauth/authorize"

# 换 token 端点（openai_auth.go:26 TokenURL）
CODEX_TOKEN_URL: str = "https://auth.openai.com/oauth/token"

# 回调地址（openai_auth.go:28 RedirectURI）
# 注意：本地并不真的起这个 server，只用来拦截重定向并从 Location 提取 code。
CODEX_REDIRECT_URI: str = "http://localhost:1455/auth/callback"

# OAuth scopes（openai_auth.go:75 GenerateAuthURL 里的 scope）
CODEX_SCOPE: str = "openid email profile offline_access"

# 输出目录名（仅名字，运行时拼到项目根；与 OUTLOOK_ACCOUNTS_FILE 同级风格）
CODEX_OUTPUT_DIRNAME: str = "codex_accounts"

# 请求超时（秒）
CODEX_REQUEST_TIMEOUT: int = 30


# ============================================================
# Codex 授权方式（2026-06-15 改造）
#
# 旧方案"复用注册的已登录 session"会撞 /choose-an-account 卡死；
# 新方案用全新干净 session 从头登录，走 OpenAI 标准风控路径
# （邮箱 OTP → 手机短信验证 → 选 workspace → 拿 code），
# 手机验证靠接码平台 GrizzlySMS 自动收码。
# ============================================================

# 注册成功后是否自动跑 Codex 授权（True=自动，False=跳过）
ENABLE_CODEX_AUTO: bool = False

# Codex OAuth 授权驱动：
#   "protocol" = 原有 curl_cffi 协议授权
#   "roxy"     = 调用 RoxyBrowser 指纹浏览器完成授权页面/手机验证/回调捕获
#   "cloak"       = 调用 CloakBrowser 完成授权页面/手机验证/回调捕获
#   "browser_use" = 调用 Browser Use Cloud 完成授权页面/手机验证/回调捕获
#   "same_as_registration" = 跟随 REGISTRATION_DRIVER
CODEX_OAUTH_DRIVER: str = "cloak"




# ============================================================
# CPA 管理接口（Codex 授权地址由 CPA 生成，本地只负责跑登录并提交回调）
# ============================================================

# 授权地址来源：
#   "cpa"   = 通过 CPA 管理接口 /v0/management/codex-auth-url 生成（推荐）
#   "local" = 使用本模块保留的本地 PKCE 生成逻辑（兼容旧方案）
CODEX_AUTH_URL_SOURCE: str = "cpa"

# CPA 管理页面或服务地址，例如 http://localhost:8317/admin/oauth
# 实际请求会取 origin，调用：
#   GET  /v0/management/codex-auth-url
#   POST /v0/management/oauth-callback
CPA_MANAGEMENT_URL: str = "http://43.167.188.22:8317/management.html"#/oauth"

# CPA 管理密钥，同时作为 Authorization: Bearer 和 X-Management-Key
CPA_MANAGEMENT_KEY: str = env_str("CPA_MANAGEMENT_KEY", "")

# CPA 管理接口请求超时（秒）
CPA_REQUEST_TIMEOUT: int = 30

# 提交 OAuth callback 给 CPA 的重试次数/基础间隔。
# 遇到 409 Timeout waiting for OAuth callback、网络超时或 5xx 时，会按同一个 callback URL 重试。
CPA_CALLBACK_SUBMIT_RETRIES: int = 5
CPA_CALLBACK_SUBMIT_RETRY_DELAY: int = 6

# CPA 未返回完整 auth json 时，是否仍在本地 codex_accounts/ 记录一份回调提交凭据
CPA_SAVE_CALLBACK_RECEIPT: bool = True

# ============================================================
# 接码平台（手机短信验证用）
# SMS_PROVIDER:
#   "grizzly" = GrizzlySMS，接口说明见 https://api.grizzlysms.com
#   "l"       = 本地 L 取号服务，接口说明见 L_API.md
# ============================================================

SMS_PROVIDER: str = "l"

# 接码 API 基址（GET handler）
SMS_API_BASE: str = "https://api.grizzlysms.com/stubs/handler_api.php"

# 接码 API 密钥（在 GrizzlySMS 后台 → 设置 获取）
# 留空时 Codex 授权的手机验证步会失败；如不需要 Codex 自动授权，把 ENABLE_CODEX_AUTO=False。
SMS_API_KEY: str = env_str("SMS_API_KEY", "")

# 服务代码：OpenAI = "dr"
SMS_SERVICE: str = "openai"

# 国家代码：葡萄牙 = "117" / 美国 = "187"
SMS_COUNTRY: str = "10"

# 单个号愿意支付的最高价格（留空=不限）。透传给 getNumber 的 maxPrice。
SMS_MAX_PRICE: str = ""

# 一个号收不到短信/被拒时，换号重试的最大次数
SMS_MAX_RETRIES: int = 10

# 单个号等待短信的最长秒数（超时则取消该号换下一个）
SMS_CODE_WAIT: int = 120

# 轮询接码平台查短信的间隔（秒）
SMS_POLL_INTERVAL: int = 5

# 接码平台 HTTP 请求超时（秒）
SMS_REQUEST_TIMEOUT: int = 30


# ============================================================
# L 取号服务（SMS_PROVIDER="l" 时使用）
# ============================================================

# L API 基址，例如本地后台：http://localhost:8788
L_API_BASE: str = "http://localhost:8788"

# L 后台授权码，对应 L_API.md 里的 Authorization: Bearer <ADMIN_AUTH_CODE>
L_ADMIN_AUTH_CODE: str = env_str("L_ADMIN_AUTH_CODE", "")

# L 返回的号码如果不含国家码，可在这里补前缀；例如美国本地 10 位号填 "1"。
# 留空则直接使用 L 返回的 item.phone。
L_PHONE_PREFIX: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'ENABLE_CODEX_AUTO': 'bool', 'CODEX_OAUTH_DRIVER': 'str', 'CODEX_AUTH_URL_SOURCE': 'str', 'CPA_MANAGEMENT_URL': 'str', 'CPA_MANAGEMENT_KEY': 'str', 'CPA_REQUEST_TIMEOUT': 'int', 'CPA_SAVE_CALLBACK_RECEIPT': 'bool', 'SMS_PROVIDER': 'str', 'SMS_COUNTRY': 'str', 'SMS_SERVICE': 'str', 'SMS_MAX_RETRIES': 'int', 'SMS_CODE_WAIT': 'int', 'SMS_API_KEY': 'str', 'L_API_BASE': 'str', 'L_ADMIN_AUTH_CODE': 'str', 'L_PHONE_PREFIX': 'str'})
