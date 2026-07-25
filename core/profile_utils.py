# -*- coding: utf-8 -*-
"""注册资料生成工具。"""

from __future__ import annotations

import random
from datetime import date, timedelta


_JAPANESE_ROMAJI_GIVEN_NAMES = (
    "Haruto", "Yuto", "Sota", "Ren", "Yuki", "Kaito", "Takumi", "Daiki",
    "Hinata", "Riku", "Sakura", "Yui", "Aoi", "Hina", "Mei", "Rin",
    "Akari", "Mio", "Koharu", "Nanami",
)
_JAPANESE_ROMAJI_FAMILY_NAMES = (
    "Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto",
    "Nakamura", "Kobayashi", "Kato", "Yoshida", "Yamada", "Sasaki", "Yamaguchi",
    "Matsumoto", "Inoue", "Shimizu", "Hayashi", "Saito", "Ishikawa",
)


def generate_display_name(locale: str = "ja", *, rng: random.Random | None = None) -> str:
    """生成符合注册接口限制的 ASCII 显示名。

    目前支持 ``ja``：使用日式罗马字名（例如 ``Haruto Sato``），不输出日文汉字或
    假名。显式传入未知地区会抛错，避免在无提示的情况下变回不确定的名称策略。
    """
    normalized = str(locale or "ja").strip().lower().replace("_", "-")
    if normalized not in {"ja", "ja-jp"}:
        raise ValueError(f"不支持的 REGISTER_NAME_LOCALE: {locale!r}（当前仅支持 ja）")
    chooser = rng or random
    return f"{chooser.choice(_JAPANESE_ROMAJI_GIVEN_NAMES)} {chooser.choice(_JAPANESE_ROMAJI_FAMILY_NAMES)}"


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
