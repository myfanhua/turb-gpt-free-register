# -*- coding: utf-8 -*-
"""
RoxyBrowser 指纹浏览器自动化注册配置。

官方文档：
- API 默认 host: http://127.0.0.1:50000
- 所有接口请求头必须带 token
- 可配合 Selenium / Puppeteer / Playwright 自动化
"""

# 注册驱动：
#   "protocol" = 原有 curl_cffi 纯协议注册
#   "roxy"     = 调用 RoxyBrowser 指纹浏览器 + Selenium 自动化注册
REGISTRATION_DRIVER: str = "cloak"

# RoxyBrowser 本地 API
ROXY_API_BASE: str = "http://127.0.0.1:50100"
ROXY_API_TOKEN: str = "637dcf52d7794e029c7556c2dbcbc07b"

# Roxy 环境/Profile ID；留空时使用 ROXY_PROFILE_CREATE_* 先创建临时环境（如果接口支持）
ROXY_PROFILE_ID: str = "-"

# Roxy 工作区 ID。Roxy 创建 Profile 时接口要求 workspaceId，必须填写。
# 可在 Roxy 工作区/团队页面或 API 返回中查看。
ROXY_WORKSPACE_ID: str = "90143"

# Roxy 项目 ID。/browser/workspace 返回 project_details.projectId；创建 Profile 时一并提交。
ROXY_PROJECT_ID: str = "97471"

# 获取团队/工作区列表接口路径。不同版本若不同，可在 WebUI 修改；客户端也会自动尝试多个常见路径。
ROXY_WORKSPACE_LIST_PATH: str = "/browser/workspace"
ROXY_WORKSPACE_LIST_METHOD: str = "GET"

# 接口路径模板。不同版本如有差异，只改这里即可。
# {profile_id} 会替换为 ROXY_PROFILE_ID。
ROXY_OPEN_PATH: str = "/browser/open"
ROXY_CLOSE_PATH: str = "/browser/close"
ROXY_CREATE_PATH: str = "/browser/create"

# 接口方法：常见 open/close 为 GET；若你的版本要求 POST，可在 WebUI/配置里改。
ROXY_OPEN_METHOD: str = "POST"
ROXY_CLOSE_METHOD: str = "POST"
ROXY_CREATE_METHOD: str = "POST"

# 打开浏览器时是否无头启动：
#   False = 显示 Roxy 浏览器窗口（便于观察/调试）
#   True  = 无头启动，不显示窗口（如果当前 Roxy 版本支持 headless）
ROXY_OPEN_HEADLESS: bool = False

# 打开浏览器时附加参数；会合并到 /browser/open 请求体，优先级高于默认值。
ROXY_OPEN_EXTRA_PARAMS: dict = {}

# Selenium 行为
ROXY_SELENIUM_TIMEOUT: int = 90
ROXY_KEEP_BROWSER_OPEN: bool = False

# Roxy API transient 错误重试。create 接口默认不重试，避免超时后重复创建孤儿环境；open/close/delete 会重试。
ROXY_API_RETRIES: int = 3
ROXY_API_RETRY_DELAY: int = 2

# 环境生命周期：
#   True  = 一号一环境：每个账号强制创建新 Profile，用完关闭并删除，不允许复用 ROXY_PROFILE_ID
#   False = 可复用 ROXY_PROFILE_ID 或只关闭不删除
ROXY_ONE_PROFILE_PER_ACCOUNT: bool = True

# 一号一环境结束后是否删除 Profile。建议保持 True。
ROXY_DELETE_PROFILE_AFTER_RUN: bool = True

# 删除环境接口路径/方法；如你的 Roxy 版本不同，只改这里。
ROXY_DELETE_PATH: str = "/browser/delete"
ROXY_DELETE_METHOD: str = "POST"

# 创建 Roxy 环境时默认系统指纹。Roxy 官方 os 枚举：Windows / macOS / Linux / IOS / Android。
ROXY_DEFAULT_OS: str = "macOS"
# 留空则使用 Roxy 对应系统的默认/最大版本；如需固定可填 15.3.2、14.7 等。
ROXY_DEFAULT_OS_VERSION: str = ""

# 创建 Roxy 环境时是否使用 config/proxy.py 的 PROXY_POOL：
#   False = 不主动给 Roxy 环境设置代理
#   True  = 每次创建环境时从 PROXY_POOL 随机取一个代理写入 proxyInfo
ROXY_CREATE_USE_PROXY_POOL: bool = False

# Roxy 代理检测通道；留空则不传 checkChannel。
ROXY_PROXY_CHECK_CHANNEL: str = "IPRust.io"

# 没有 ROXY_PROFILE_ID 时创建环境的最小 payload；按你的 Roxy 版本字段调整。
ROXY_PROFILE_CREATE_PAYLOAD: dict = {
    "name": "gpt-free-register",
    "os": "macOS",
}


# Roxy Codex 授权等待 callback 的最长秒数
ROXY_CODEX_CALLBACK_TIMEOUT: int = 180
