"""ChatGPT 换绑邮箱纯协议（复用 iCloud registration_core，不改其源码）。"""

from .pipeline import RebindError, RebindResult, run_rebind_email

__all__ = ["RebindError", "RebindResult", "run_rebind_email"]
