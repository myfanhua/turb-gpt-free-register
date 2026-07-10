# -*- coding: utf-8 -*-
"""注册资料生成工具。"""

from __future__ import annotations

import random
from datetime import date, timedelta


def _shift_year_safe(day: date, years: int) -> date:
    """按年偏移日期；遇到 2 月 29 日且目标年非闰年时回退到 2 月 28 日。"""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, month=2, day=28)


def generate_random_birthday(min_age: int = 18, max_age: int = 65) -> str:
    """
    生成年龄在 [min_age, max_age] 闭区间内的随机生日，格式 YYYY-MM-DD。

    例如默认会在“今天满 65 岁”到“今天满 18 岁”之间随机取一天。
    """
    if min_age < 0 or max_age < min_age:
        raise ValueError(f"年龄范围无效: min_age={min_age}, max_age={max_age}")

    today = date.today()
    oldest = _shift_year_safe(today, -max_age)
    youngest = _shift_year_safe(today, -min_age)
    span_days = (youngest - oldest).days
    birthday = oldest + timedelta(days=random.randint(0, span_days))
    return birthday.isoformat()
