# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from typing import Callable

from config import roxybrowser as roxy_cfg
from core.email_provider import wait_for_otp
from core.roxy_registration import (
    _clear_otp_inputs,
    _click_continue,
    _click_passwordless_signup_if_present,
    _click_resend_email_otp,
    _fetch_chatgpt_session,
    _has_access_token,
    _is_email_verification_page,
    _open_roxy_registration_browser,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
)
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)


class AccessTokenRecoveryStopped(RuntimeError):
    pass


class PhoneVerificationRequired(RuntimeError):
    pass


def _is_phone_verification_page(driver) -> bool:
    try:
        state = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', autocomplete: el.getAttribute('autocomplete') || ''
        }));
        const forms = [...document.querySelectorAll('form')].map(f => f.getAttribute('action') || '');
        return {url: location.href, inputs, forms};
        """) or {}
    except Exception:
        state = {"url": str(getattr(driver, "current_url", "") or "")}
    if not isinstance(state, dict):
        state = {"url": str(getattr(driver, "current_url", "") or "")}
    url = str(state.get("url") or "").lower()
    attrs = " ".join(
        " ".join(str(item.get(key) or "") for key in ("type", "name", "id", "autocomplete"))
        for item in (state.get("inputs") or [])
    ).lower()
    forms = " ".join(str(value or "") for value in (state.get("forms") or [])).lower()
    return (
        "phone-verification" in url
        or "add-phone" in url
        or "phone-verification" in forms
        or "add-phone" in forms
        or "type tel" in attrs
        or "autocomplete tel" in attrs
    )


def _check_abort(driver, should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise AccessTokenRecoveryStopped("用户手动停止")
    if _is_phone_verification_page(driver):
        raise PhoneVerificationRequired("网页版登录要求手机号验证，本任务已停止")


def _wait_for_otp_page_or_session(
    driver,
    should_stop: Callable[[], bool],
    timeout: int = 30,
) -> str:
    end = time.time() + timeout
    while time.time() < end:
        _check_abort(driver, should_stop)
        if _has_access_token(driver):
            return "logged_in"
        if _is_email_verification_page(driver):
            return "otp"
        time.sleep(0.5)
    raise RuntimeError("点击一次性验证码登录后未进入验证码页")


def _is_otp_timeout(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(value in text for value in ("timeout", "timed out", "超时", "未收到", "no otp"))


def _wait_for_otp_stoppable(
    email: str,
    *,
    after_ts: float,
    should_stop: Callable[[], bool],
    timeout: int = 180,
) -> str:
    end = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < end:
        if should_stop():
            raise AccessTokenRecoveryStopped("用户手动停止")
        remaining = max(1, int(end - time.time()))
        try:
            return wait_for_otp(
                email,
                after_ts=after_ts,
                max_wait=min(10, remaining),
                poll_interval=2,
            )
        except Exception as exc:
            last_exc = exc
            if not _is_otp_timeout(exc):
                raise
    raise RuntimeError(f"等待邮箱验证码超时: {last_exc}")


def _open_browser(
    *,
    proxy: str | None,
    device_id: str,
    should_stop: Callable[[], bool],
):
    client = RoxyBrowserClient()

    def stop_checker() -> None:
        if should_stop():
            raise AccessTokenRecoveryStopped("用户手动停止")

    opened, driver = _open_roxy_registration_browser(
        client,
        device_id=device_id,
        proxy=proxy,
        stop_checker=stop_checker,
    )
    return client, opened, driver


def run_roxy_access_token_recovery(
    *,
    email: str,
    proxy: str | None,
    device_id: str,
    should_stop: Callable[[], bool],
) -> dict:
    client = None
    opened = None
    driver = None
    try:
        client, opened, driver = _open_browser(
            proxy=proxy,
            device_id=device_id,
            should_stop=should_stop,
        )
        abort_checker = lambda: _check_abort(driver, should_stop)
        otp_after_ts = time.time()
        state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            allow_login_password=True,
            abort_checker=abort_checker,
        )
        if state == "password":
            raise RuntimeError("已有账号登录进入创建账号密码页，已停止以避免重新注册")
        if state == "login_password":
            clicked = _click_passwordless_signup_if_present(driver)
            if not clicked.get("ok"):
                raise RuntimeError("登录密码页没有可用的一次性验证码登录入口")
            state = _wait_for_otp_page_or_session(driver, should_stop)

        if state != "logged_in":
            for attempt in range(1, 4):
                abort_checker()
                code = _wait_for_otp_stoppable(
                    email,
                    after_ts=otp_after_ts,
                    should_stop=should_stop,
                )
                _clear_otp_inputs(driver)
                _type_otp(driver, code)
                _click_continue(driver)
                outcome = _wait_after_email_otp_submit(
                    driver,
                    timeout=30,
                    abort_checker=abort_checker,
                )
                if outcome == "accepted":
                    break
                if outcome == "account_deactivated":
                    raise RuntimeError("OpenAI 账号已删除或停用: account_deactivated")
                if attempt >= 3:
                    raise RuntimeError("邮箱验证码连续错误或过期，已达到最大重试次数")
                otp_after_ts = time.time()
                resend = _click_resend_email_otp(
                    driver,
                    timeout=25,
                    abort_checker=abort_checker,
                )
                if resend.get("advanced"):
                    break

        session_info = _fetch_chatgpt_session(
            driver,
            timeout=120,
            abort_checker=abort_checker,
        )
        if not str(session_info.get("accessToken") or "").strip():
            raise RuntimeError("/api/auth/session 未返回 accessToken")
        return {
            "session_info": session_info,
            "device_id": str(opened.account_device_id or device_id),
            "proxy_used": opened.registration_proxy or proxy,
            "profile_id": opened.profile_id,
        }
    finally:
        keep_open = bool(roxy_cfg.ROXY_KEEP_BROWSER_OPEN)
        if driver is not None and not keep_open:
            try:
                driver.quit()
            except Exception:
                pass
        if client is not None and opened is not None and not keep_open:
            try:
                client.cleanup_profile(opened)
            except Exception:
                logger.exception("[补AT] 清理 Roxy 环境失败: profile=%s", opened.profile_id)
