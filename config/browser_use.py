# -*- coding: utf-8 -*-
"""
Browser Use Cloud 配置。

文档：
- https://docs.browser-use.com/cloud/browser/stealth
- https://docs.browser-use.com/cloud/browser/playwright-puppeteer-selenium

用法：
  1. 在 config/roxybrowser.py 设置 REGISTRATION_DRIVER = "browser_use"
  2. 在 .env 填入 BROWSER_USE_API_KEY（也可用 WebUI 密钥字段写入 .env）
  3. 推荐先关 Codex：ENABLE_CODEX_AUTO = False
"""
from config.env_loader import env_str

# Browser Use API Key（Cloud Dashboard 创建；优先读 .env / 环境变量）
BROWSER_USE_API_KEY: str = env_str("BROWSER_USE_API_KEY", "")

# 连接方式：
#   "cdp_url" = 直接用官方 CDP websocket（推荐，最简单）
#   "sdk"     = 先调 REST 创建 session（预留；默认仍走 cdp_url）
BROWSER_USE_CONNECT_MODE: str = "cdp_url"

# CDP 连接地址模板。{api_key}/{proxy_country_code}/{profile_id} 会按需替换或追加 query。
BROWSER_USE_CDP_BASE: str = "wss://connect.browser-use.com"

# 可选 REST API 根地址（以后若改走显式 create/stop session 用）
BROWSER_USE_API_BASE: str = "https://api.browser-use.com/api/v2"

# 代理国家代码，两位小写，例如 jp / us / sg / de；留空则用 Browser Use 默认出口
BROWSER_USE_PROXY_COUNTRY_CODE: str = "jp"

# 是否使用 Browser Use 内置代理。False 时尽量不强制 cloud proxy（仍取决于服务端默认）
BROWSER_USE_USE_PROXY: bool = True

# 可选：固定 Browser Use profileId，用于复用 cookies/localStorage。
# 个人批量注册建议留空，让每次新会话更干净。
BROWSER_USE_PROFILE_ID: str = ""

# Playwright / 页面超时
BROWSER_USE_TIMEOUT: int = 90
BROWSER_USE_NAVIGATION_TIMEOUT: int = 90

# 任务结束后是否主动断开 CDP
BROWSER_USE_KEEP_BROWSER_OPEN: bool = False

# 额外 CDP query 参数，会合并到 connect URL
# 例：{"timeout": "300"}
BROWSER_USE_EXTRA_QUERY: dict = {}

# 打开的起始注册页
BROWSER_USE_START_URL: str = "https://chatgpt.com/auth/login"
