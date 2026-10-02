# -*- coding: utf-8 -*-
"""Skyvern 云端浏览器注册入口。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.browser_use_registration import run_browser_use_registration


def run_skyvern_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
    exclude_emails=None,
) -> dict:
    return run_browser_use_registration(
        email=email,
        name=name,
        birthday=birthday,
        proxy=proxy,
        otp_code=otp_code,
        batch_dir=batch_dir,
        on_email_acquired=on_email_acquired,
        exclude_emails=exclude_emails,
        cloud_provider="skyvern",
    )
