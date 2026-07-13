# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入项目根 `.env`，不再修改 `config/*.py`。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 用 `.env` 覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权（全新session+接码），落盘 codex-邮箱.json",
    },
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "注册方式",
        "label": "注册驱动", "help": "protocol=纯协议；roxy=RoxyBrowser；cloak=CloakBrowser；browser_use=Browser Use Cloud+Playwright",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak License", "help": "Pro license；留空使用免费 binary",
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak用户目录", "help": "留空使用临时上下文；填写路径则持久化 cookies/cache",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Browser Use API Key", "help": "保存在 .env（BROWSER_USE_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Profile ID", "help": "可选。填写则复用 Browser Use profile 的 cookies/localStorage；批量建议留空",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API Key", "help": "保存在 .env（ROXY_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "CPA / Codex",
        "label": "Codex授权驱动", "help": "protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；browser_use=用 Browser Use Cloud；same_as_registration=跟随注册驱动",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "可填单个或多个，逗号分隔并按顺序兜底：outlook,generic_api,cloudflare_domain,gptmail",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名", "help": "你的 Cloudflare 域名，如 mydomain.com",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱地址", "help": "接收 Cloudflare 转发邮件的 QQ 邮箱，如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱 IMAP 授权码", "help": "保存在 .env（QQ_IMAP_PASSWORD），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "每行一个代理 URL，留空行会被忽略；为空则不使用代理",
    },
    # ---- 接码平台 ----
    # ---- CPA / Codex 授权 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "CPA / Codex",
        "label": "授权地址来源", "help": "cpa=由 CPA 管理接口生成授权地址；local=旧版本地生成 PKCE 授权地址",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "CPA / Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "CPA / Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "CPA / Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "CPA / Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "接码通道", "help": "grizzly 或 l；l 使用 L_API.md 定义的本地取号服务",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "传给接码平台的 country；GrizzlySMS 常用：美国=187 / 葡萄牙=117 / 智利=151；L 通道按 L 后台配置填写",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务代码", "help": "传给接码平台的 service；GrizzlySMS OpenAI=dr；L 通道按 L 后台配置填写",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "API 密钥", "help": "保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in env_file_values:
            value = _coerce_raw_value(env_file_values[key], fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = "env"
        item["value"] = value
        out.append(item)
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 .env 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines)
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写项目根 `.env`。"""
    from config.env_loader import write_env_values, load_env

    updated, ignored = [], []
    env_updates: dict[str, str] = {}

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)

    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "env_updated": env_updated}
