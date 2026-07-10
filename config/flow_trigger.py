# -*- coding: utf-8 -*-
"""
注册成功后自动触发 Flow 的配置项。
设置 ENABLE_FLOW_TRIGGER = False 可完全跳过此步骤。
"""

# 是否启用自动触发 Flow（False = 跳过，不影响注册结果）
ENABLE_FLOW_TRIGGER: bool = False

# Flow 触发接口地址
FLOW_TRIGGER_URL: str = ""

# Bearer Token（Authorization 头）
FLOW_TRIGGER_BEARER: str = ""

# Cookie 字符串
FLOW_TRIGGER_COOKIE: str = ""

# 发送的 JSON payload（会把 access_token 注入进去）
FLOW_TRIGGER_PAYLOAD: dict = {}

# 请求超时（秒）
FLOW_TRIGGER_TIMEOUT: int = 15