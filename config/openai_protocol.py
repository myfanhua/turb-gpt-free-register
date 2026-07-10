# -*- coding: utf-8 -*-
"""
OpenAI / ChatGPT OAuth 协议固定参数

来自抓包，OpenAI 自己的 client_id 是固定值。
SENTINEL_SV 是 sdk.js 的版本号，会随 OpenAI 更新而变化，
更新时去 https://sentinel.openai.com/sentinel/<version>/sdk.js 找当前版本。
"""

# OAuth 客户端 ID（固定）
OPENAI_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"

# OAuth scopes
OPENAI_SCOPE = (
    "openid email profile offline_access "
    "model.request model.read "
    "organization.read organization.write"
)

# OAuth audience
OPENAI_AUDIENCE = "https://api.openai.com/v1"

# OAuth 回调（chatgpt.com 端）
OPENAI_REDIRECT_URI = "https://chatgpt.com/api/auth/callback/openai"

# Sentinel SDK 版本号（影响 sentinel iframe URL 与 referer header）
SENTINEL_SV = "20260219f9f6"

# ChatGPT 页面 build 标识（用于 Sentinel p[6] / documentElement data-build 模拟）
OPENAI_BUILD_ID = "c/prod-4987068829830ddc3ae6683bd4e633f61b79dec9/_"
