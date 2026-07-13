# -*- coding: utf-8 -*-
"""从项目根目录 .env 加载密钥/敏感配置。

设计目标：
  - 重要 API Key 不进 git 跟踪的 config/*.py 默认值
  - config 模块启动 / reload 时读取环境变量
  - WebUI 可读写 .env 中的密钥字段
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOADED = False

# 统一管理：env key -> 说明（.env.example 用）
SECRET_ENV_KEYS: dict[str, str] = {
    "BROWSER_USE_API_KEY": "Browser Use Cloud API Key",
    "ROXY_API_TOKEN": "RoxyBrowser 本地 API Token",
    "QQ_IMAP_PASSWORD": "QQ 邮箱 IMAP 授权码（不是 QQ 密码）",
    "GPTMAIL_API_KEY": "GPTMail API Key",
    "CPA_MANAGEMENT_KEY": "CPA 管理接口密钥",
    "SMS_API_KEY": "接码平台 API Key（如 GrizzlySMS）",
    "L_ADMIN_AUTH_CODE": "本地 L 接码服务 ADMIN_AUTH_CODE",
}


def env_path() -> Path:
    return _ENV_PATH


def load_env(*, override: bool = False) -> Path:
    """加载项目根 .env 到进程环境。可重复调用（reload 时用 override=True）。

    优先使用 python-dotenv；未安装时使用本文件内置的轻量 parser，避免配置读取强依赖。
    """
    global _LOADED
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        if _ENV_PATH.exists():
            for key, value in read_env_file().items():
                if override or key not in os.environ:
                    os.environ[key] = value
        _LOADED = True
        return _ENV_PATH

    if _ENV_PATH.exists():
        load_dotenv(dotenv_path=_ENV_PATH, override=override)
    else:
        # 仍然允许系统环境变量生效
        load_dotenv(override=override)
    _LOADED = True
    return _ENV_PATH


def ensure_loaded() -> None:
    if not _LOADED:
        load_env(override=False)


def env_str(key: str, default: str = "") -> str:
    ensure_loaded()
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _escape_env_value(value: str) -> str:
    # 统一双引号，避免空格/特殊字符问题
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
    )
    return f'"{escaped}"'


def read_env_file() -> dict[str, str]:
    """解析 .env 文件为 dict（不依赖 os.environ）。"""
    if not _ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
            val = val.replace("\\n", "\n").replace("\\\"", '"').replace("\\\\", "\\")
        out[key] = val
    return out


def write_env_values(updates: dict[str, str]) -> list[str]:
    """更新 .env 中的若干 key；不存在则追加。返回实际写入的 key 列表。"""
    if not updates:
        return []

    existing_lines: list[str] = []
    if _ENV_PATH.exists():
        existing_lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    remaining = {str(k): ("" if v is None else str(v)) for k, v in updates.items()}
    written: list[str] = []
    out_lines: list[str] = []
    key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

    for line in existing_lines:
        m = key_re.match(line)
        if not m:
            out_lines.append(line)
            continue
        key = m.group(1)
        if key in remaining:
            out_lines.append(f"{key}={_escape_env_value(remaining.pop(key))}")
            written.append(key)
        else:
            out_lines.append(line)

    if remaining:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append("# ---- updated by WebUI / config.env_loader ----")
        for key, value in remaining.items():
            out_lines.append(f"{key}={_escape_env_value(value)}")
            written.append(key)

    text = "\n".join(out_lines).rstrip() + "\n"
    tmp = _ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(_ENV_PATH)

    # 让当前进程立刻看到新值
    load_env(override=True)
    return written


def _coerce_env_value(raw: str, default, vtype: str | None = None):
    if vtype is None:
        if isinstance(default, bool):
            vtype = "bool"
        elif isinstance(default, int) and not isinstance(default, bool):
            vtype = "int"
        elif isinstance(default, float):
            vtype = "float"
        elif isinstance(default, (list, tuple)):
            vtype = "list_str_multiline"
        else:
            vtype = "str"
    if vtype == "bool":
        return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
    if vtype == "int":
        return int(str(raw).strip())
    if vtype == "float":
        return float(str(raw).strip())
    if vtype == "list_str_multiline":
        text = str(raw)
        # 兼容旧值：PROXY_POOL='["http://..."]'
        try:
            import ast
            val = ast.literal_eval(text)
            if isinstance(val, (list, tuple)):
                return [str(x).strip() for x in val if str(x).strip()]
        except Exception:
            pass
        return [line.strip() for line in text.splitlines() if line.strip()]
    return str(raw).strip()


def env_value(key: str, default=None, vtype: str | None = None):
    ensure_loaded()
    raw = os.getenv(key)
    # `.env.example` 和 WebUI 里常见 `KEY=` / `KEY=""` 这种空配置。
    # 空值表示“未配置，使用 config/*.py 里的默认值”，否则 bool 默认 True
    # 会被空字符串误覆盖成 False，str/list 默认值也会被误清空。
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return _coerce_env_value(raw, default, vtype)
    except Exception:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    return bool(env_value(key, default, "bool"))


def env_int(key: str, default: int = 0) -> int:
    return int(env_value(key, default, "int"))


def env_float(key: str, default: float = 0.0) -> float:
    return float(env_value(key, default, "float"))


def env_list(key: str, default: list[str] | None = None) -> list[str]:
    return list(env_value(key, default or [], "list_str_multiline"))


def apply_env_overrides(namespace: dict, schema: dict[str, str] | None = None) -> None:
    """用 .env/环境变量覆盖模块 globals() 中的配置常量。

    schema: {KEY: type}，type 支持 bool/int/float/str/list_str_multiline。
    没传 schema 时，会对 namespace 里已有的大写常量按默认值类型推断。
    """
    ensure_loaded()
    keys = schema.keys() if schema else [k for k in namespace if k.isupper()]
    for key in keys:
        if os.getenv(key) is None:
            continue
        default = namespace.get(key)
        vtype = schema.get(key) if schema else None
        namespace[key] = env_value(key, default, vtype)
