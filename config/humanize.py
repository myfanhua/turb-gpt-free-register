# -*- coding: utf-8 -*-
"""
人工操作节奏配置。

协议请求本身很快；真实浏览器人工操作通常会有页面加载、阅读、输入、切换邮箱
等停顿。这里集中配置轻量随机延迟，避免全流程固定节拍。
"""

# 总开关。关闭后 delay() 直接返回。
ENABLE_HUMANIZE_DELAY = True

# 延迟倍率；批量跑得太慢时可调小到 0.5。
HUMANIZE_DELAY_FACTOR = 1.0

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
}
