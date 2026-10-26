# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 注册密码（OTP-only 流程已不需要，留作备用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 注册成功落库后是否自动查询套餐/Plus 资格。
# 关闭后不会在注册完成后立刻访问 backend-api/accounts/check，后续可在账号列表手动查询。
AUTO_PLAN_CHECK_AFTER_REGISTER = False

# 纯协议注册指纹生命周期：
#   False = 每次注册任务创建全新的 device/session/浏览器画像（默认）
#   True  = 同一邮箱重复注册或补跑时复用稳定指纹；不同邮箱仍彼此独立
PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL = False

# 只有注册认证链明确要求 add-phone 时才运行；与 Codex 自动授权开关独立。
# 复用 config.codex 的 SMS_* 供应商和有界取号/等待配置。
PROTOCOL_REGISTRATION_PHONE_ENABLED = False
# 同号等待期间有界重发；0 次可禁用重发，错误码重试不额外延长单号等待上限。
PROTOCOL_PHONE_RESEND_INTERVAL = 20
PROTOCOL_PHONE_MAX_RESENDS = 3
PROTOCOL_PHONE_CODE_ATTEMPTS = 3

# 注册成功并拿到 accessToken 后，在浏览器里随机停留一段时间再关闭连接。
# 格式：最小秒,最大秒。设为 "0,0" 表示不额外停留。
POST_REGISTER_DWELL_SECONDS_RANGE = "5,15"

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_NAME': 'str',
    'AUTO_PLAN_CHECK_AFTER_REGISTER': 'bool',
    'PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL': 'bool',
    'PROTOCOL_REGISTRATION_PHONE_ENABLED': 'bool',
    'PROTOCOL_PHONE_RESEND_INTERVAL': 'int',
    'PROTOCOL_PHONE_MAX_RESENDS': 'int',
    'PROTOCOL_PHONE_CODE_ATTEMPTS': 'int',
    'POST_REGISTER_DWELL_SECONDS_RANGE': 'str',
})
