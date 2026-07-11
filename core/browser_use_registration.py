# -*- coding: utf-8 -*-
"""
Browser Use Cloud + Playwright 注册驱动。

目标：
  - 不依赖本机 RoxyBrowser
  - 通过 Browser Use stealth Chromium + 可选 residential proxy 完成 ChatGPT 注册
  - 复用本仓库邮箱 OTP / 账号落盘逻辑
  - 默认不做 Codex（需要时可后续再接）
"""
from __future__ import annotations

import logging
import random
import string
import time
from pathlib import Path
from typing import Any

from config import browser_use as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.browser_use_client import BrowserUseClient
from core.email_provider import resolve_email_source, wait_for_otp
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except ImportError:
        return


def _generate_password(length: int = 14) -> str:
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = "!@#$%^&*"
    chars = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(symbols),
    ]
    pool = upper + lower + digits + symbols
    chars.extend(random.choice(pool) for _ in range(max(0, length - len(chars))))
    random.shuffle(chars)
    return "".join(chars)


def _registration_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, "REGISTER_PASSWORD", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return _generate_password()


def _timeout_ms(seconds: int | None = None) -> int:
    value = int(seconds or getattr(_cfg, "BROWSER_USE_TIMEOUT", 90) or 90)
    return max(5, value) * 1000


def _page_url(page) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _visible_locator(page, selectors: list[str], timeout_ms: int = 1500):
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if loc.count() == 0:
                continue
            if loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


def _fill_first(page, selectors: list[str], value: str, timeout_ms: int | None = None) -> bool:
    end = time.time() + ((timeout_ms or _timeout_ms()) / 1000)
    last_err = None
    while time.time() < end:
        loc = _visible_locator(page, selectors, timeout_ms=800)
        if loc is not None:
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=2000)
                loc.fill(value, timeout=5000)
                return True
            except Exception as exc:
                last_err = exc
                # React 受控输入兜底
                try:
                    loc.evaluate(
                        """(el, value) => {
                          const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                          if (setter) setter.call(el, value); else el.value = value;
                          el.dispatchEvent(new Event('input', {bubbles:true}));
                          el.dispatchEvent(new Event('change', {bubbles:true}));
                        }""",
                        value,
                    )
                    return True
                except Exception as exc2:
                    last_err = exc2
        time.sleep(0.3)
    if last_err:
        logger.debug("[BrowserUse] fill failed: %s", last_err)
    return False


def _click_first(page, selectors: list[str], timeout_ms: int | None = None) -> bool:
    end = time.time() + ((timeout_ms or _timeout_ms()) / 1000)
    while time.time() < end:
        loc = _visible_locator(page, selectors, timeout_ms=800)
        if loc is not None:
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=3000)
                return True
            except Exception:
                try:
                    loc.evaluate("el => el.click()")
                    return True
                except Exception:
                    pass
        time.sleep(0.3)
    return False


def _maybe_accept_cookies(page) -> None:
    _click_first(
        page,
        [
            "button:has-text('Accept')",
            "button:has-text('Accept all')",
            "button:has-text('同意')",
            "button:has-text('接受')",
            "button:has-text('I agree')",
        ],
        timeout_ms=2500,
    )


def _assert_not_external_idp(page, stage: str) -> None:
    url = _page_url(page).lower()
    bad_hosts = (
        "accounts.google.com",
        "appleid.apple.com",
        "login.microsoftonline.com",
        "github.com/login",
        "facebook.com/login",
    )
    if any(h in url for h in bad_hosts):
        raise RuntimeError(f"[BrowserUse] {stage} 误入第三方登录：{url}")


def _type_email(page, email: str) -> None:
    # 有的登录页要先点 “Continue with email”
    _click_first(
        page,
        [
            "button[data-testid*='email' i]",
            "button[data-provider='email']",
            "button:has-text('Continue with email')",
            "button:has-text('Sign up with email')",
            "button:has-text('使用邮箱')",
            "a:has-text('Continue with email')",
        ],
        timeout_ms=4000,
    )
    _assert_not_external_idp(page, "邮箱入口")

    ok = _fill_first(
        page,
        [
            "input[type='email']",
            "input[name='email']",
            "input[name='username']",
            "input[autocomplete='email']",
            "input[id*='email' i]",
            "input[placeholder*='email' i]",
            "input[placeholder*='邮箱']",
        ],
        email,
        timeout_ms=_timeout_ms(),
    )
    if not ok:
        raise RuntimeError("找不到邮箱输入框")

    if not _click_first(
        page,
        [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('继续')",
            "button:has-text('下一步')",
            "form button",
        ],
        timeout_ms=8000,
    ):
        # 回车提交
        page.keyboard.press("Enter")
    human_delay("form")


def _is_password_page(page) -> bool:
    url = _page_url(page).lower()
    if any(x in url for x in ("/create-account/password", "/u/signup/password", "/signup/password")):
        return True
    if "/log-in/password" in url:
        return False
    loc = _visible_locator(
        page,
        [
            "input[type='password']",
            "input[name='password']",
            "input[autocomplete='new-password']",
        ],
        timeout_ms=500,
    )
    return loc is not None and "email-verification" not in url


def _is_email_verification_page(page) -> bool:
    url = _page_url(page).lower()
    if "email-verification" in url or "email_otp" in url or "verify" in url and "email" in url:
        return True
    loc = _visible_locator(
        page,
        [
            "input[name='code']",
            "input[autocomplete='one-time-code']",
            "input[inputmode='numeric']",
            "input[aria-label*='code' i]",
            "input[placeholder*='code' i]",
        ],
        timeout_ms=500,
    )
    return loc is not None


def _fill_password_if_present(page, email: str, timeout: int = 25) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        if _is_email_verification_page(page):
            return None
        if not _is_password_page(page):
            time.sleep(0.4)
            continue
        password = _registration_password()
        logger.info("[BrowserUse] 检测到密码页，设置密码（%s 位）：%s", len(password), email)
        ok = _fill_first(
            page,
            [
                "input[type='password']",
                "input[name='password']",
                "input[autocomplete='new-password']",
                "input[autocomplete='current-password']",
            ],
            password,
            timeout_ms=8000,
        )
        if not ok:
            raise RuntimeError("密码页找到了，但无法填写密码")
        if not _click_first(
            page,
            [
                "button[type='submit']",
                "button:has-text('Continue')",
                "button:has-text('Next')",
                "button:has-text('继续')",
                "button:has-text('创建')",
                "form button",
            ],
            timeout_ms=8000,
        ):
            page.keyboard.press("Enter")
        human_delay("form")
        return password
    return None


def _type_otp(page, code: str) -> None:
    code = str(code or "").strip()
    if not code:
        raise RuntimeError("OTP 为空")

    # 单框
    if _fill_first(
        page,
        [
            "input[name='code']",
            "input[autocomplete='one-time-code']",
            "input[name='otp']",
            "input[aria-label*='code' i]",
            "input[placeholder*='code' i]",
            "input[inputmode='numeric']",
        ],
        code,
        timeout_ms=5000,
    ):
        return

    # 多分框 6 位
    boxes = page.locator("input[maxlength='1'], input[data-index], input[aria-label*='digit' i]")
    try:
        count = boxes.count()
    except Exception:
        count = 0
    if count >= len(code):
        for i, ch in enumerate(code):
            boxes.nth(i).fill(ch)
        return
    raise RuntimeError("找不到 OTP 输入框")


def _clear_otp_inputs(page) -> None:
    try:
        page.evaluate(
            """() => {
              for (const el of document.querySelectorAll('input')) {
                const t = (el.type || '').toLowerCase();
                const n = (el.name || '').toLowerCase();
                const a = (el.autocomplete || '').toLowerCase();
                if (t === 'tel' || t === 'number' || t === 'text' || n.includes('code') || n.includes('otp') || a.includes('one-time')) {
                  const proto = window.HTMLInputElement.prototype;
                  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                  if (setter) setter.call(el, ''); else el.value = '';
                  el.dispatchEvent(new Event('input', {bubbles:true}));
                  el.dispatchEvent(new Event('change', {bubbles:true}));
                }
              }
            }"""
        )
    except Exception:
        pass


def _click_continue(page) -> None:
    if not _click_first(
        page,
        [
            "button[type='submit']",
            "button:has-text('Continue')",
            "button:has-text('Verify')",
            "button:has-text('Submit')",
            "button:has-text('继续')",
            "button:has-text('验证')",
            "form button",
        ],
        timeout_ms=5000,
    ):
        page.keyboard.press("Enter")


def _click_resend_otp(page) -> bool:
    return _click_first(
        page,
        [
            "button:has-text('Resend')",
            "button:has-text('Send again')",
            "button:has-text('重新发送')",
            "a:has-text('Resend')",
            "button:has-text('没有收到')",
        ],
        timeout_ms=5000,
    )


def _wait_after_otp(page, timeout: int = 12) -> str:
    """返回 accepted / invalid / unknown。"""
    end = time.time() + timeout
    while time.time() < end:
        url = _page_url(page).lower()
        body = ""
        try:
            body = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            pass
        if any(x in url for x in ("about-you", "profile", "chatgpt.com", "create-account/about")):
            return "accepted"
        if any(x in body for x in ("incorrect", "invalid", "expired", "错误", "过期", "无效")) and _is_email_verification_page(page):
            return "invalid"
        if "chatgpt.com" in url and "auth" not in url:
            return "accepted"
        time.sleep(0.5)
    return "unknown"


def _fill_birthday_fields(page, birthday: str) -> None:
    # birthday: YYYY-MM-DD
    try:
        year, month, day = [int(x) for x in birthday.split("-")]
    except Exception as exc:
        raise RuntimeError(f"生日格式应为 YYYY-MM-DD: {birthday}") from exc

    # 年龄数字页
    age = max(18, min(60, 2026 - year))
    if _fill_first(
        page,
        [
            "input[name='age']",
            "input[id*='age' i]",
            "input[aria-label*='age' i]",
            "input[placeholder*='age' i]",
            "input[type='number']",
        ],
        str(age),
        timeout_ms=2500,
    ):
        return

    # 年月日 select / spinbutton 尽量覆盖
    y, m, d = str(year), str(month), str(day)
    # year/month/day inputs
    for selectors, value in (
        ([
            "select[name*='year' i]",
            "input[name*='year' i]",
            "input[aria-label*='year' i]",
            "[data-type='year'] input",
        ], y),
        ([
            "select[name*='month' i]",
            "input[name*='month' i]",
            "input[aria-label*='month' i]",
            "[data-type='month'] input",
        ], m),
        ([
            "select[name*='day' i]",
            "input[name*='day' i]",
            "input[aria-label*='day' i]",
            "[data-type='day'] input",
        ], d),
    ):
        loc = _visible_locator(page, selectors, timeout_ms=800)
        if loc is None:
            continue
        try:
            tag = (loc.evaluate("el => el.tagName") or "").lower()
            if tag == "select":
                try:
                    loc.select_option(value=value)
                except Exception:
                    loc.select_option(label=value)
            else:
                loc.fill(value)
        except Exception:
            try:
                loc.fill(value)
            except Exception:
                pass


def _complete_profile_page(page, name: str, birthday: str, timeout: int = 60) -> bool:
    end = time.time() + timeout
    submitted = False
    while time.time() < end:
        _check_manual_stop()
        url = _page_url(page).lower()
        if "chatgpt.com" in url and "auth.openai.com" not in url and "about-you" not in url:
            return True
        looks_profile = any(x in url for x in ("about-you", "profile", "create-account/about", "signup/profile"))
        name_loc = _visible_locator(
            page,
            [
                "input[name='name']",
                "input[autocomplete='name']",
                "input[name='full_name']",
                "input[name='username']",
                "input[placeholder*='name' i]",
                "input[aria-label*='name' i]",
            ],
            timeout_ms=500,
        )
        if looks_profile or name_loc is not None:
            logger.info("[BrowserUse] 资料页：填写昵称/生日")
            if name_loc is not None:
                try:
                    name_loc.fill(name)
                except Exception:
                    _fill_first(page, ["input[name='name']", "input[autocomplete='name']"], name, timeout_ms=3000)
            else:
                _fill_first(
                    page,
                    [
                        "input[name='name']",
                        "input[autocomplete='name']",
                        "input[name='full_name']",
                        "input[placeholder*='name' i]",
                    ],
                    name,
                    timeout_ms=3000,
                )
            _fill_birthday_fields(page, birthday)
            # 同意协议勾选（若有）
            try:
                checks = page.locator("input[type='checkbox']")
                for i in range(min(checks.count(), 6)):
                    box = checks.nth(i)
                    if box.is_visible() and not box.is_checked():
                        box.check(force=True)
            except Exception:
                pass
            if _click_first(
                page,
                [
                    "button[type='submit']",
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                    "button:has-text('Done')",
                    "button:has-text('继续')",
                    "button:has-text('完成')",
                    "form button",
                ],
                timeout_ms=5000,
            ):
                submitted = True
                human_delay("form")
                time.sleep(1.0)
                continue
        time.sleep(0.6)
    return submitted


def _fetch_chatgpt_session(page, timeout: int = 120) -> dict:
    end = time.time() + timeout
    last = None
    navigated = False
    while time.time() < end:
        _check_manual_stop()
        url = _page_url(page).lower()
        if "chatgpt.com" not in url:
            time.sleep(1.5)
            continue
        navigated = True
        try:
            data = page.evaluate(
                """async () => {
                  const r = await fetch('/api/auth/session', {credentials: 'include'});
                  return await r.json();
                }"""
            )
            last = data
            if isinstance(data, dict) and data.get("accessToken"):
                logger.info("[BrowserUse] /api/auth/session 已返回 accessToken")
                return data
            logger.info("[BrowserUse] 等待 accessToken，keys=%s", list((data or {}).keys()) if isinstance(data, dict) else type(data))
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)

    if not navigated:
        logger.info("[BrowserUse] 未自动跳转 chatgpt.com，主动打开并读取 session")
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
        time.sleep(4)
        try:
            data = page.evaluate(
                """async () => {
                  const r = await fetch('/api/auth/session', {credentials: 'include'});
                  return await r.json();
                }"""
            )
            if isinstance(data, dict) and data.get("accessToken"):
                return data
            last = data
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last)[:800]}")


def run_browser_use_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir: Path | None = None,
) -> dict:
    """Browser Use Cloud 注册入口。proxy 参数保留兼容，但默认使用 Browser Use 自带代理。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "缺少 playwright。请先执行: uv pip install playwright --python .venv/bin/python"
        ) from exc

    client = BrowserUseClient()
    session_info_open = client.open_session()
    create_acknowledged = False
    openai_password: str | None = None
    browser = None
    context = None
    page = None

    logger.info(
        "[BrowserUse] 开始注册：%s proxyCountry=%s profileId=%s local_proxy_arg=%s",
        email,
        session_info_open.proxy_country_code or "-",
        session_info_open.profile_id or "-",
        "yes" if proxy else "no",
    )

    try:
        with sync_playwright() as p:
            logger.info("[BrowserUse] 连接 CDP ...")
            browser = p.chromium.connect_over_cdp(session_info_open.connect_url)
            # Browser Use 通常已有默认 context/page
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(_timeout_ms())
            page.set_default_navigation_timeout(_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))

            start_url = str(getattr(_cfg, "BROWSER_USE_START_URL", "https://chatgpt.com/auth/login") or "https://chatgpt.com/auth/login")
            logger.info("[BrowserUse] 打开登录页：%s", start_url)
            page.goto(start_url, wait_until="domcontentloaded")
            human_delay("navigate")
            _maybe_accept_cookies(page)
            _check_manual_stop()

            _type_email(page, email)
            logger.info("[BrowserUse] 已提交邮箱：%s", email)
            _assert_not_external_idp(page, "提交邮箱后")
            _check_manual_stop()

            openai_password = _fill_password_if_present(page, email, timeout=25)
            _check_manual_stop()

            otp_after_ts = time.time()
            current_otp = otp_code
            max_otp_attempts = 3
            for otp_attempt in range(1, max_otp_attempts + 1):
                # 等验证码页出现
                wait_end = time.time() + 30
                while time.time() < wait_end and not _is_email_verification_page(page):
                    if any(x in _page_url(page).lower() for x in ("about-you", "profile", "chatgpt.com/")):
                        break
                    time.sleep(0.4)

                if current_otp is None:
                    logger.info("[BrowserUse][OTP] 等待验证码：%s（%s/%s）", email, otp_attempt, max_otp_attempts)
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                logger.info("[BrowserUse][OTP] 收到验证码：%s", current_otp)
                _clear_otp_inputs(page)
                _type_otp(page, current_otp)
                human_delay("otp_input")
                try:
                    _click_continue(page)
                except Exception as exc:
                    logger.info("[BrowserUse][OTP] 提交按钮未找到，继续观察页面：%s", str(exc)[:120])
                _check_manual_stop()

                outcome = _wait_after_otp(page, timeout=12)
                if outcome in ("accepted", "unknown"):
                    # unknown 也继续尝试资料页/session
                    break
                if otp_attempt >= max_otp_attempts:
                    raise RuntimeError("邮箱验证码连续错误/过期")
                logger.warning("[BrowserUse][OTP] 验证码可能无效，尝试重发（%s/%s）", otp_attempt + 1, max_otp_attempts)
                otp_after_ts = time.time()
                _click_resend_otp(page)
                human_delay("api")
                current_otp = None

            logger.info("[BrowserUse] 处理资料页/登录态")
            profile_submitted = _complete_profile_page(page, name, birthday, timeout=60)
            if profile_submitted:
                create_acknowledged = True
                human_delay("post_auth")

            session_info = _fetch_chatgpt_session(page, timeout=120)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("注册流程结束但未拿到 accessToken")
            create_acknowledged = True
            logger.info("[BrowserUse] 已拿到 accessToken：%s", email)

            if _twofa_cfg.ENABLE_2FA:
                logger.warning("[BrowserUse] 当前路径暂不自动设置 2FA，已跳过")
            totp_secret = None

            # 个人少量账号场景默认不在此驱动里强跑 Codex/SMS。
            codex_result = {
                "status": "skipped",
                "ok": True,
                "message": "Browser Use 注册驱动默认跳过 Codex；可稍后用补跑/协议路径处理",
            }
            try:
                from config import codex as _codex_cfg
                if bool(getattr(_codex_cfg, "ENABLE_CODEX", True)) and bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", True)):
                    logger.warning(
                        "[BrowserUse][Codex] 已启用 Codex 自动授权，但 browser_use 驱动暂未实现手机验证闭环；"
                        "本次标记为 skipped，请关闭 ENABLE_CODEX_AUTO 或之后补跑"
                    )
                    codex_result = {
                        "status": "skipped",
                        "ok": True,
                        "message": "browser_use 暂不自动 Codex（避免卡在手机验证）",
                    }
            except Exception:
                pass

            account_id = save_account_data(
                email=email,
                access_token=access_token,
                totp_secret=totp_secret,
                email_source=resolve_email_source(email),
                proxy_used=proxy or f"browser_use:{session_info_open.proxy_country_code or 'default'}",
                batch_dir=batch_dir,
                extra={
                    "user": session_info.get("user"),
                    "account": session_info.get("account"),
                    "expires": session_info.get("expires"),
                    "browser_use": {
                        "proxy_country_code": session_info_open.proxy_country_code,
                        "profile_id": session_info_open.profile_id,
                        "connect": session_info_open.raw,
                    },
                    "registration_password": openai_password,
                    "codex": codex_result,
                },
            )
            return {
                "success": True,
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "codex": codex_result,
                "error": None,
            }
    except Exception as exc:
        logger.error("[BrowserUse] 注册失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[BrowserUse] 失败详情", exc_info=True)
        try:
            from core.email_provider import release_email
            release_email(
                email,
                status="failed" if create_acknowledged else "available",
                note=f"BrowserUse注册失败: {str(exc)[:180]}",
            )
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        # CDP 远端会话：关闭 browser 连接；Browser Use 侧通常会随断开回收。
        if not bool(getattr(_cfg, "BROWSER_USE_KEEP_BROWSER_OPEN", False)):
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
