# -*- coding: utf-8 -*-
"""
人工操作节奏配置。

协议请求本身很快；真实浏览器人工操作通常会有页面加载、阅读、输入、切换邮箱
等停顿。这里集中配置轻量随机延迟，避免全流程固定节拍。
"""
from config.env_loader import apply_env_overrides

# 总开关。关闭后 delay() 直接返回。
ENABLE_HUMANIZE_DELAY = True

# 延迟倍率；批量跑得太慢时可调小到 0.5。
HUMANIZE_DELAY_FACTOR = 1.0

# Roxy/Cloak 浏览器自动化动作随机化。开启后会使用更接近人工的点击/输入：
# - 点击前轻微滚动、移动到元素内随机位置、短暂停顿再点击
# - 输入按字符/小段随机节奏，不再一次性整串 send_keys
# - 页面打开后做少量随机停顿/鼠标移动
ENABLE_HUMANIZE_BROWSER_ACTIONS = True

# 节奏画像：fast / balanced / careful。careful 主要增加提交前复核，
# 不会额外制造误按或错误输入。
HUMANIZE_PACING_PROFILE = "balanced"

# 提交邮箱、密码、OTP 和资料页前保留一次有预算的短暂复核。
HUMANIZE_REVIEW_BEFORE_SUBMIT = True

# 鼠标从上一落点沿多段轨迹移动到元素内部，而不是直接跳到目标点。
HUMANIZE_MOUSE_TRAJECTORY = True

# 每类动作的随机停顿区间（秒）。
HUMANIZE_DELAYS = {
    # 普通 API 间隔：看起来像页面 JS 发完一个请求后处理状态。
    "api": (0.45, 1.35),
    # 页面跳转 / 重定向后等页面稳定。
    "navigate": (1.2, 3.2),
    # Sentinel / Turnstile / PoW 相关，给 SDK 运行和 UI 等待留时间。
    "challenge": (0.8, 2.4),
    # 邮箱验证码到达后，模拟用户切回页面和输入。
    "otp_input": (2.5, 8.0),
    # 填写姓名生日等表单。
    "form": (1.8, 5.0),
    # 注册完成后进入应用、拉 session。
    "post_auth": (1.5, 4.0),
    # 并发任务错峰。
    "job_stagger": (0.4, 1.8),
    # 点击前观察/移动鼠标。
    "click": (0.15, 0.85),
    # 关键提交前的短暂复核；每个阶段/按钮最多一次。
    "review": (0.35, 1.20),
    # 单字符输入间隔。
    "keystroke": (0.035, 0.18),
    # 输入中偶尔停顿。
    "typing_pause": (0.18, 0.75),
    # 页面打开后的短暂观察。
    "page_warmup": (0.7, 2.2),
}

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_HUMANIZE_DELAY': 'bool',
    'HUMANIZE_DELAY_FACTOR': 'float',
    'ENABLE_HUMANIZE_BROWSER_ACTIONS': 'bool',
    'HUMANIZE_PACING_PROFILE': 'str',
    'HUMANIZE_REVIEW_BEFORE_SUBMIT': 'bool',
    'HUMANIZE_MOUSE_TRAJECTORY': 'bool',
})
