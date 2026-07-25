# -*- coding: utf-8 -*-
"""patch5: 删除其余文件中的 synthetic 引用。"""
from pathlib import Path

# --- account_export.py：删注册后自动转化块 ---
p = Path("core/account_export.py")
data = p.read_bytes()
start = data.find("        try:\n            from core import synthetic_auth\n".encode("utf-8"))
end_marker = '            logger.warning("[SyntheticAuth] 自动转化失败但不回滚账号: %s, %s", email, type(exc).__name__)\n'.encode("utf-8")
end = data.find(end_marker)
assert start != -1 and end != -1 and start < end, (start, end)
data = data[:start] + data[end + len(end_marker):]
p.write_bytes(data)
import ast
ast.parse(data.decode("utf-8"))
print("account_export.py ok")

# --- cloakbrowser_registration.py ---
p = Path("core/cloakbrowser_registration.py")
data = p.read_bytes()
old = '            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)) and not bool(getattr(_codex_cfg, "CODEX_SYNTHETIC_AUTH_ENABLE", False)):  # synthetic 转化开启时跳过接码 OAuth\n'.encode("utf-8")
new = '            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):\n'.encode("utf-8")
assert data.count(old) == 1, data.count(old)
data = data.replace(old, new)
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("cloakbrowser_registration.py ok")

# --- browser_use_registration.py ---
p = Path("core/browser_use_registration.py")
data = p.read_bytes()
old = '                codex_auto_enabled = codex_auto_enabled and not bool(getattr(_codex_cfg, "CODEX_SYNTHETIC_AUTH_ENABLE", False))  # synthetic 转化开启时跳过接码 OAuth\n'.encode("utf-8")
assert data.count(old) == 1, data.count(old)
data = data.replace(old, b"")
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("browser_use_registration.py ok")

# --- config/codex.py：删开关定义 + env override 项 ---
p = Path("config/codex.py")
data = p.read_bytes()
s = data.find("# 免手机验证 synthetic auth 转化（学习自 codex-auth-helper）：\n".encode("utf-8"))
e = data.find("CODEX_SYNTHETIC_AUTH_ENABLE: bool = False\n".encode("utf-8"))
assert s != -1 and e != -1 and s < e, (s, e)
data = data[:s] + data[e + len("CODEX_SYNTHETIC_AUTH_ENABLE: bool = False\n".encode("utf-8")):]
old = "'ENABLE_CODEX_AUTO': 'bool', 'CODEX_SYNTHETIC_AUTH_ENABLE': 'bool', ".encode("utf-8")
new = "'ENABLE_CODEX_AUTO': 'bool', ".encode("utf-8")
assert data.count(old) == 1, data.count(old)
data = data.replace(old, new)
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("config/codex.py ok")

# --- webui/config_editor.py：删配置项 ---
p = Path("webui/config_editor.py")
data = p.read_bytes()
s = data.find('        "key": "CODEX_SYNTHETIC_AUTH_ENABLE",'.encode("utf-8"))
assert s != -1
# 该项为 dict 元素 { ... }, 找到元素开始（前一个 { 所在行）
line_start = data.rfind(b"\n", 0, s) + 1
elem_start = data.rfind(b"{", 0, s)
# 找该 dict 结束 "}," 之后
elem_end = data.find(b"},", s)
assert elem_start != -1 and elem_end != -1
# 从行首删到 "}," 后（含换行）
tail = elem_end + 2
if data[tail:tail+2] == b"\r\n":
    tail += 2
elif data[tail:tail+1] == b"\n":
    tail += 1
data = data[:line_start] + data[tail:]
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("config_editor.py ok")

# --- core/codex_oauth_service.py：注释措辞更新（synthetic 已删除） ---
p = Path("core/codex_oauth_service.py")
data = p.read_bytes()
old = "- 「免手机验证 synthetic 转化」产出的 auth.json 用的是 web session accessToken\n".encode("utf-8")
new = "- 已删除的「免手机验证 synthetic 转化」产出的 auth.json 用的是 web session accessToken\n".encode("utf-8")
assert data.count(old) == 1
data = data.replace(old, new)
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("codex_oauth_service.py ok")
