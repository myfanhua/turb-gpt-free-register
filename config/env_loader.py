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
    "CPA_MANAGEMENT_KEY": "CPA 管理接口密钥",
    "SMS_API_KEY": "接码平台 API Key（如 GrizzlySMS）",
    "L_ADMIN_AUTH_CODE": "本地 L 接码服务 ADMIN_AUTH_CODE",
}


def env_path() -> Path:
    return _ENV_PATH


def load_env(*, override: bool = False) -> Path:
    """加载项目根 .env 到进程环境。可重复调用（reload 时用 override=True）。"""
    global _LOADED
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 python-dotenv。请执行: uv pip install python-dotenv --python .venv/bin/python"
        ) from exc

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
    if value is None:
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
