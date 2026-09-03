from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ensure_icloud_on_path() -> Path:
    """将独立包内的 registration_core 父目录加入 sys.path。

    可通过环境变量 REBIND_REG_ROOT 覆盖（指向含 registration_core 的目录）。
    """
    override = (os.environ.get("REBIND_REG_ROOT") or os.environ.get("ICLOUD_REG_ROOT") or "").strip()
    root = Path(override).expanduser().resolve() if override else ROOT
    if not (root / "registration_core").is_dir():
        raise RuntimeError(
            "找不到 registration_core。请确认独立包完整，或设置 REBIND_REG_ROOT 指向含 registration_core 的目录。"
            f" current={root}"
        )
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    return root
