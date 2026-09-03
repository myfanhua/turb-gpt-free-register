#!/usr/bin/env python3
"""ChatGPT 换绑邮箱纯协议 CLI。

流程：旧邮箱+密码+TOTP 登录 → change_email begin/verify → 新邮箱+旧密码+旧TOTP 重登 → 导出 login_bundle
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebind_core.pipeline import run_rebind_email


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ChatGPT 换绑邮箱纯协议")
    p.add_argument("--old-email", default=_env("REBIND_OLD_EMAIL"))
    p.add_argument("--password", default=_env("REBIND_PASSWORD"))
    p.add_argument("--totp-secret", default=_env("REBIND_TOTP_SECRET"))
    p.add_argument("--new-email", default=_env("REBIND_NEW_EMAIL"))
    p.add_argument("--mail-api", default=_env("REBIND_MAIL_API"), help="新邮箱收信 API 完整 URL")
    p.add_argument("--proxy", default=_env("REBIND_PROXY"))
    p.add_argument("--out", default=_env("REBIND_OUT", str(ROOT / "outputs" / "session_export")))
    p.add_argument("--mail-timeout", type=float, default=float(_env("REBIND_MAIL_TIMEOUT") or "120"))
    p.add_argument("--yes", action="store_true", help="非交互：缺参直接失败")
    return p.parse_args(argv)


def prompt_if_needed(args: argparse.Namespace) -> argparse.Namespace:
    if args.yes:
        return args
    if not args.old_email:
        args.old_email = input("旧邮箱: ").strip()
    if not args.password:
        args.password = getpass.getpass("密码: ")
    if not args.totp_secret:
        args.totp_secret = getpass.getpass("TOTP 密钥(Base32): ").strip()
    if not args.new_email:
        args.new_email = input("新邮箱: ").strip()
    if not args.mail_api:
        args.mail_api = input("新邮箱收信 API URL: ").strip()
    return args


def main(argv: list[str] | None = None) -> int:
    args = prompt_if_needed(parse_args(argv))
    missing = [
        name
        for name, val in (
            ("old-email", args.old_email),
            ("password", args.password),
            ("totp-secret", args.totp_secret),
            ("new-email", args.new_email),
            ("mail-api", args.mail_api),
        )
        if not val
    ]
    if missing:
        print(f"缺少参数: {', '.join(missing)}", file=sys.stderr)
        return 2

    print("=== ChatGPT 换绑邮箱纯协议 ===")
    print(f"旧邮箱: {args.old_email}")
    print(f"新邮箱: {args.new_email}")
    print(f"代理: {args.proxy or '(直连)'}")
    print(f"输出: {args.out}")

    result = run_rebind_email(
        old_email=args.old_email,
        password=args.password,
        totp_secret=args.totp_secret,
        new_email=args.new_email,
        mail_api=args.mail_api,
        proxy=args.proxy or None,
        out_dir=args.out,
        mail_timeout=args.mail_timeout,
    )
    print()
    if not result.ok:
        print(f"失败 [{result.code}] {result.message}")
        print(f"trace: {result.run_dir}")
        return 1

    print("成功")
    print(f"session email: {result.session_email}")
    print(f"AT: {result.access_token_masked}")
    print(f"bundle: {result.bundle_path}")
    print(f"run dir: {result.run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
