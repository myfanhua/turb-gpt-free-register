# -*- coding: utf-8 -*-
"""
Codex OAuth 单独验证脚本（2026-06-15 重写：全新 session + 接码方案）。

用途：
    不重新注册，直接拿一个**已注册成功的邮箱**单独补跑 Codex 授权，
    走"全新干净 session → 邮箱 OTP → 手机短信验证(接码) → 选 workspace → 拿 code → 换 token → 落盘"。
    首跑用于校准各 /api/accounts/* 接口的响应处理。

用法：
    python tools/test_codex_oauth.py --email <已注册邮箱> [--verbose]

前提：
    - 该邮箱在邮箱池（用于注册的邮箱.json）里有完整凭证（client_id/refresh_token），用于收邮箱 OTP
    - config/codex.py 里 ENABLE_CODEX_AUTO=True，接码配置（SMS_*）已填好
    - 会真实消耗：该邮箱一封邮箱 OTP + 一个接码短信（约 $0.13）

输出：
    - 全程 [Codex] / [SMS] 日志
    - 成功则落盘 codex_accounts/codex-邮箱.json，并打印 PASS
    - 失败打印 FAIL + 完整堆栈，便于定位卡在哪一步
"""
import argparse
import logging
import sys
from pathlib import Path

# 让 tools/ 脚本能 import 到项目根的 core / config
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.codex_oauth import run_codex_oauth

logger = logging.getLogger("codex_test")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # 让 core 模块的 DEBUG 也显示（看 redirect 跟随等细节）
    logging.getLogger("core").setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Codex OAuth 单独验证（全新 session + 接码）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--email",
        required=True,
        help="已注册成功的邮箱（必须在邮箱池里有凭证，用于收邮箱 OTP）",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="代理地址（不传则从 PROXY_POOL 随机抽）",
    )
    parser.add_argument("--verbose", action="store_true", help="显示 DEBUG 日志（含 redirect 跟随细节）")
    args = parser.parse_args()
    _setup_logging(args.verbose)

    logger.info("=" * 60)
    logger.info(f"[测试] 单独补跑 Codex 授权：{args.email}")
    logger.info("=" * 60)

    result = run_codex_oauth(args.email, proxy=args.proxy, force=True)

    logger.info("-" * 60)
    logger.info(f"[测试] 结果：status={result.get('status')}, ok={result.get('ok')}")
    if result.get("ok"):
        logger.info("=" * 60)
        logger.info("✅✅✅ [PASS] Codex OAuth 验证通过 ✅✅✅")
        logger.info(f"    邮箱     = {result.get('email')}")
        logger.info(f"    落盘文件 = {result.get('file_path')}")
        logger.info(f"    回调URL  = {result.get('callback_url')}")
        logger.info(f"    备注     = {result.get('message')}")
        logger.info("=" * 60)
        return 0
    else:
        logger.error("=" * 60)
        logger.error(f"❌ [FAIL] Codex OAuth 验证失败")
        logger.error(f"    status  = {result.get('status')}")
        logger.error(f"    message = {result.get('message')}")
        logger.error("=" * 60)
        logger.error("提示：把上面 [Codex]/[SMS] 的日志发给开发，定位卡在哪一步。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
