"""Stub: browser mode is not included in the standalone rebind package."""
from __future__ import annotations

class _BrowserUnavailable(RuntimeError):
    pass

def __getattr__(name: str):
    raise _BrowserUnavailable(
        f"registration_core.{browser_traffic}.{name} 不可用：独立换绑包仅含纯协议登录，不含浏览器注册模块"
    )
