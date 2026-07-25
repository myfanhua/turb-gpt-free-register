# -*- coding: utf-8 -*-
"""注册后工作流配置；默认关闭，生产发送需已验证协议契约。"""
import json
from config.env_loader import apply_env_overrides, env_str

POST_REGISTER_ENABLE = False
MESSAGE_COUNT = 0
MESSAGE_LIST = []  # JSON 数组，例如 ["第一条", "第二条"]
POST_REGISTER_CONVERSATION_ID = ""
POST_REGISTER_TIMEOUT = 30

apply_env_overrides(globals(), {"POST_REGISTER_ENABLE": "bool", "MESSAGE_COUNT": "int", "POST_REGISTER_CONVERSATION_ID": "str", "POST_REGISTER_TIMEOUT": "int"})
_raw_messages = env_str("MESSAGE_LIST", "")
if _raw_messages:
    try:
        MESSAGE_LIST = json.loads(_raw_messages)
    except json.JSONDecodeError:
        MESSAGE_LIST = _raw_messages
