# -*- coding: utf-8 -*-
"""
Outlook 邮箱账号池配置。

注册邮箱与 OTP 均只走 Outlook 账号池：
    1. 把邮箱素材写入项目根目录 `用于注册的邮箱.txt`
    2. 每行格式：email====password====clientId====refreshToken
    3. 运行注册时会自动导入新增邮箱
"""

# True: REGISTER_EMAIL 留空时从 Outlook 账号池自动获取邮箱，OTP 自动收取
# False: 走人工输入邮箱 + 人工填 OTP 的流程
USE_EMAIL_SERVICE = False

# 可选值（也可以用英文逗号配置多个，按顺序兜底，例如 "outlook,generic_api"）：
#   "outlook"           — 外购 Outlook 账号池 + mail.chatai.codes 远端取信
#   "cloudflare_domain" — Cloudflare 域名邮箱（转发到 QQ 邮箱），通过 IMAP 取信
#   "generic_api"       — 通用 API 取码邮箱池（邮箱----取码地址）
EMAIL_SOURCE = "outlook,generic_api"


# ============================================================
# Outlook 模式（外购账号池 + 取信服务）
# ============================================================

OUTLOOK_ACCOUNTS_FILE = "用于注册的邮箱.txt"

# 取邮件 API 的根 URL（双协议 graph + imap，自动回退）
OUTLOOK_API_BASE = "https://mail.chatai.codes"


# ============================================================
# OTP 轮询参数
# ============================================================

OTP_POLL_INTERVAL = 3
OTP_MAX_WAIT = 90

# Outlook 双协议取件：抓到一封 OTP 后再多等多少秒看是否有更晚到达的邮件。
OTP_SETTLE_SECONDS = 5


# ============================================================
# Cloudflare 域名邮箱模式（转发到 QQ 邮箱，通过 IMAP 取信）
# ============================================================

# 你的 Cloudflare 域名，如 "mydomain.com"
# 注册时会自动生成 random@mydomain.com 作为注册邮箱
EMAIL_DOMAIN = ""

# QQ 邮箱 IMAP 服务器地址（固定为 imap.qq.com）
QQ_IMAP_SERVER = "imap.qq.com"

# QQ 邮箱 IMAP 端口（SSL）
QQ_IMAP_PORT = 993

# QQ 邮箱地址（接收 Cloudflare 转发的邮件），如 "123456@qq.com"
QQ_EMAIL = ""

# QQ 邮箱 IMAP 授权码（在 QQ 邮箱网页版 → 设置 → 账户 → POP3/IMAP/SMTP 服务 中生成）
# 注意：这是 16 位授权码，不是 QQ 密码
QQ_IMAP_PASSWORD = ""
