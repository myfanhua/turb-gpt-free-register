# -*- coding: utf-8 -*-
"""patch5b: 删除其余 synthetic 引用（CRLF 行尾修正版）。"""
from pathlib import Path
import ast

# --- cloakbrowser_registration.py（CRLF） ---
p = Path("core/cloakbrowser_registration.py")
data = p.read_bytes()
old = '            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)) and not bool(getattr(_codex_cfg, "CODEX_SYNTHETIC_AUTH_ENABLE", False)):  # synthetic 转化开启时跳过接码 OAuth\r\n'.encode("utf-8")
new = '            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):\r\n'.encode("utf-8")
assert data.count(old) == 1, data.count(old)
data = data.replace(old, new)
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("cloakbrowser_registration.py ok")

# --- browser_use_registration.py（该行 CRLF） ---
p = Path("core/browser_use_registration.py")
data = p.read_bytes()
old = '                codex_auto_enabled = codex_auto_enabled and not bool(getattr(_codex_cfg, "CODEX_SYNTHETIC_AUTH_ENABLE", False))  # synthetic 转化开启时跳过接码 OAuth\r\n'.encode("utf-8")
assert data.count(old) == 1, data.count(old)
data = data.replace(old, b"")
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("browser_use_registration.py ok")

# --- config/codex.py ---
p = Path("config/codex.py")
data = p.read_bytes()
s = data.find("# 免手机验证 synthetic auth 转化（学习自 codex-auth-helper）：".encode("utf-8"))
assert s != -1
# 行首
s = data.rfind(b"\n", 0, s) + 1
e_marker = "CODEX_SYNTHETIC_AUTH_ENABLE: bool = False".encode("utf-8")
e = data.find(e_marker, s)
assert e != -1
eol = data.find(b"\n", e) + 1
data = data[:s] + data[eol:]
old = "'ENABLE_CODEX_AUTO': 'bool', 'CODEX_SYNTHETIC_AUTH_ENABLE': 'bool', ".encode("utf-8")
assert data.count(old) == 1, data.count(old)
data = data.replace(old, "'ENABLE_CODEX_AUTO': 'bool', ".encode("utf-8"))
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("config/codex.py ok")

# --- webui/config_editor.py：删配置项 dict 元素 ---
p = Path("webui/config_editor.py")
data = p.read_bytes()
s = data.find('"key": "CODEX_SYNTHETIC_AUTH_ENABLE",'.encode("utf-8"))
assert s != -1
line_start = data.rfind(b"\n", 0, s) + 1
elem_end = data.find(b"},", s)
assert elem_end != -1
tail = elem_end + 2
if data[tail:tail+2] == b"\r\n":
    tail += 2
elif data[tail:tail+1] == b"\n":
    tail += 1
data = data[:line_start] + data[tail:]
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("config_editor.py ok")

# --- core/codex_oauth_service.py：注释措辞 ---
p = Path("core/codex_oauth_service.py")
data = p.read_bytes()
old = "- 「免手机验证 synthetic 转化」产出的 auth.json 用的是 web session accessToken\n".encode("utf-8")
assert data.count(old) == 1
data = data.replace(old, "- 已删除的「免手机验证 synthetic 转化」产出的 auth.json 用的是 web session accessToken\n".encode("utf-8"))
p.write_bytes(data)
ast.parse(data.decode("utf-8"))
print("codex_oauth_service.py ok")
