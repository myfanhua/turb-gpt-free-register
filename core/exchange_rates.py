"""Latest published foreign-exchange rates used for account cost display."""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen

_RATE_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY"
_CACHE_TTL_SECONDS = 900
_FAILURE_RETRY_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 3
_LOCK = threading.Lock()
_CACHE: tuple[float, Decimal, str] | None = None
_RETRY_AFTER = 0.0


def get_usd_cny_rate() -> tuple[Decimal, str] | None:
    """Return the latest published USD/CNY rate and its publication date."""
    global _CACHE, _RETRY_AFTER
    with _LOCK:
        now = time.monotonic()
        if _CACHE and now - _CACHE[0] < _CACHE_TTL_SECONDS:
            return _CACHE[1], _CACHE[2]
        if now < _RETRY_AFTER:
            return None

        request = Request(_RATE_URL, headers={"User-Agent": "turb-gpt-register/1.0"})
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rate = Decimal(str(payload["rates"]["CNY"]))
            rate_date = str(payload["date"])
            if not rate.is_finite() or rate <= 0 or len(rate_date) != 10:
                raise ValueError("invalid USD/CNY rate response")
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation, json.JSONDecodeError):
            _RETRY_AFTER = now + _FAILURE_RETRY_SECONDS
            return None

        _CACHE = (now, rate, rate_date)
        _RETRY_AFTER = 0.0
        return rate, rate_date


def _reset_cache_for_tests() -> None:
    global _CACHE, _RETRY_AFTER
    with _LOCK:
        _CACHE = None
        _RETRY_AFTER = 0.0
