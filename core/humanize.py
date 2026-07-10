# -*- coding: utf-8 -*-
"""随机化操作节奏，让协议流程更接近人工浏览器操作。"""
import logging
import random
import time

logger = logging.getLogger(__name__)


def delay(kind: str = "api", *, minimum: float | None = None, maximum: float | None = None) -> float:
    """
    按配置随机 sleep，返回实际 sleep 秒数。

    Args:
        kind: HUMANIZE_DELAYS 的 key。
        minimum/maximum: 临时覆盖区间。
    """
    try:
        from config import humanize as _cfg
        if not getattr(_cfg, "ENABLE_HUMANIZE_DELAY", True):
            return 0.0
        if minimum is None or maximum is None:
            lo, hi = getattr(_cfg, "HUMANIZE_DELAYS", {}).get(kind, (0.4, 1.2))
            minimum = lo if minimum is None else minimum
            maximum = hi if maximum is None else maximum
        factor = float(getattr(_cfg, "HUMANIZE_DELAY_FACTOR", 1.0) or 1.0)
    except Exception:
        minimum = 0.4 if minimum is None else minimum
        maximum = 1.2 if maximum is None else maximum
        factor = 1.0

    lo = max(0.0, float(minimum) * factor)
    hi = max(lo, float(maximum) * factor)
    seconds = random.uniform(lo, hi)
    logger.debug(f"[Humanize] delay kind={kind}, seconds={seconds:.2f}")
    time.sleep(seconds)
    return seconds
