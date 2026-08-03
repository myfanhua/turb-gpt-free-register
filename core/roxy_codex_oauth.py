# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器执行 Codex OAuth 授权。"""
from __future__ import annotations

import logging
import random
import time
from contextvars import ContextVar
from urllib.parse import urlparse

from config import roxybrowser as _roxy_cfg
from core.email_provider import wait_for_otp
from core.humanize import delay as human_delay
from core import sms_provider
from core.sms_country_router import PreferredCountrySelector
from core.openai_auth import AccountUnusableError, detect_account_unusable_response_body
from core.roxybrowser_client import RoxyBrowserClient
from core.roxy_registration import (
    _build_driver,
    _center_browser_window,
    _click_any,
    _click_continue,
    _find_any,
    _maybe_accept,
    _type_any,
    _type_email_address,
    _submit_email_step,
    _click_email_entry_option,
    _type_otp,
    _clear_otp_inputs,
    _email_otp_page_state,
    _is_email_verification_page,
    _click_passwordless_signup_if_present,
)

_base_logger = logging.getLogger(__name__)
_CODEX_BROWSER_KIND: ContextVar[str] = ContextVar("codex_browser_kind", default="Roxy")


def _codex_prefix() -> str:
    return f"[Codex][{_CODEX_BROWSER_KIND.get()}]"


def _codex_driver_name() -> str:
    return _CODEX_BROWSER_KIND.get()


def _detect_browser_kind(opened=None) -> str:
    try:
        raw = getattr(opened, "raw", None) or {}
        if isinstance(raw, dict) and str(raw.get("driver") or "").lower().startswith("cloak"):
            return "Cloak"
    except Exception:
        pass
    return "Roxy"


def _resolve_token_exchange_proxy(opened, explicit_proxy: str | None) -> str | None:
    """Prefer the exact proxy attached to the Roxy profile for token exchange."""
    registration_proxy = str(getattr(opened, "registration_proxy", "") or "").strip()
    if registration_proxy:
        return registration_proxy
    fallback = str(explicit_proxy or "").strip()
    return fallback or None


def _preflight_token_exchange_transport(session, token_url: str) -> int:
    """Confirm the token endpoint is reachable before email/SMS work begins."""
    response = session.get(str(token_url or "").strip(), allow_redirects=False)
    status = int(getattr(response, "status_code", 0) or 0)
    if status in (407, 502, 503, 504):
        raise RuntimeError(f"token_proxy_preflight_failed: status={status}")
    logger.info("[Codex][Browser] Token 交换代理预检通过：status=%s", status or "-")
    return status


class _CodexLogger:
    """把流程内部统一占位前缀替换成当前真实浏览器类型。"""
    def __init__(self, base):
        self._base = base

    def _msg(self, msg):
        return str(msg).replace("[Codex][Browser]", _codex_prefix())

    def debug(self, msg, *args, **kwargs):
        return self._base.debug(self._msg(msg), *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        return self._base.info(self._msg(msg), *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        return self._base.warning(self._msg(msg), *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        return self._base.error(self._msg(msg), *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        return self._base.exception(self._msg(msg), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base, name)


logger = _CodexLogger(_base_logger)


def _is_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.port == 1455
        and parsed.path == "/auth/callback"
    )


def _extract_callback_url_from_page(driver) -> str:
    """从当前页面提取 OAuth callback URL。

    浏览器跳转到 http://localhost:1455/auth/callback?... 时，本地没有服务监听会显示
    chrome-error://chromewebdata/。地址栏可能变成 chrome-error，但 Chromium 的
    performance navigation entry 仍保留原始 callback URL，可直接提取后提交 CPA。
    """
    try:
        current = str(driver.current_url or "")
        if _is_callback_url(current):
            return current
    except Exception:
        pass
    try:
        urls = driver.execute_script(r"""
        const out = [];
        const push = v => { if (v && typeof v === 'string') out.push(v); };
        try { push(location.href); } catch (e) {}
        try { push(document.URL); } catch (e) {}
        try { push(document.documentURI); } catch (e) {}
        try { for (const e of performance.getEntriesByType('navigation')) push(e.name); } catch (e) {}
        try { for (const e of performance.getEntries()) push(e.name); } catch (e) {}
        return [...new Set(out)];
        """) or []
        for url in urls:
            if _is_callback_url(str(url)):
                logger.info("[Codex][Browser] 已从浏览器性能记录提取 callback URL：%s", str(url)[:160])
                return str(url)
    except Exception as exc:
        logger.debug("[Codex][Browser] 从页面提取 callback URL 失败：%s", exc)
    return ""


def _extract_callback_url_from_any_window(driver) -> str:
    found = _extract_callback_url_from_page(driver)
    if found:
        return found
    try:
        for handle in list(getattr(driver, "window_handles", []) or []):
            try:
                driver.switch_to.window(handle)
                found = _extract_callback_url_from_page(driver)
                if found:
                    return found
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _wait_for_callback(driver, timeout: int | None = None) -> str:
    end = time.time() + (timeout or int(_roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT))
    last_url = ""
    while time.time() < end:
        try:
            current = str(driver.current_url or "")
            if current != last_url:
                logger.debug("[Codex][Browser] 当前 URL: %s", current)
                last_url = current
            callback = _extract_callback_url_from_any_window(driver)
            if callback:
                return callback
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"等待 Codex callback 超时，最后 URL={last_url}")


def _click_if_present(driver, selectors: list[str], timeout: int = 3) -> bool:
    try:
        _click_any(driver, selectors, timeout=timeout)
        return True
    except Exception:
        return False


def _maybe_click_passwordless_after_email(driver, email: str, timeout: int = 18) -> None:
    """
    Codex OAuth 提交邮箱后也可能跳到 /log-in/password 或 /create-account/password。
    优先点击“使用一次性验证码/one-time code”入口，进入邮箱验证码页。
    """
    end = time.time() + timeout
    last_url = ""
    clicked = False
    while time.time() < end:
        try:
            if _is_email_verification_page(driver):
                if clicked:
                    logger.info("[Codex][Browser] 一次性验证码入口已进入邮箱验证码页")
                return
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] 提交邮箱后检测密码/OTP 跳转：url=%s", url or "-")
                last_url = url
            lower = url.lower()
            if any(x in lower for x in ("phone", "workspace", "consent", "authorize", "localhost:1455")):
                return
            if "/password" in lower or "auth.openai.com" in lower:
                result = _click_passwordless_signup_if_present(driver)
                if result.get("ok"):
                    clicked = True
                    logger.info("[Codex][Browser] 已点击一次性验证码入口：email=%s detail=%s", email, result)
                    human_delay("form")
                    continue
        except Exception as exc:
            logger.debug("[Codex][Browser] 密码页一次性验证码入口探测失败：%s", str(exc)[:140])
        time.sleep(0.5)
    if clicked:
        logger.info("[Codex][Browser] 已点击一次性验证码入口，未立即检测到 OTP 页，继续后续 OTP 轮询")


def _fill_email_and_otp(driver, email: str, otp_provider, auth_url: str) -> None:
    otp_after_ts = time.time()
    logger.info("[Codex][Browser] 打开授权地址")
    logger.info("[Codex][Browser] 完整授权地址: %s", auth_url)
    driver.get(auth_url)
    human_delay("navigate")
    logger.info("[Codex][Browser] 授权页加载完成，检查是否需要邮箱登录")
    _maybe_accept(driver)

    # 可能已经处于账号选择/授权页；如果有邮箱输入框则完整登录。
    # 非日本出口时按钮文案/顺序会变，不能按可见文字点“继续”，否则可能误点 Google。
    try:
        _type_email_address(driver, email, timeout=12)
        logger.info("[Codex][Browser] 已填写邮箱：%s", email)
        human_delay("form")
        _submit_email_step(driver)
        logger.info("[Codex][Browser] 已提交邮箱，等待邮箱 OTP 页面")
        _maybe_click_passwordless_after_email(driver, email, timeout=18)
    except Exception as exc:
        logger.info("[Codex][Browser] 未检测到邮箱输入框，可能已登录或进入下一步：%s", str(exc)[:120])
        return

    # 提交邮箱后不再执行任何全局“继续/授权/分支”兜底点击；后续只等待验证码页。
    # 避免页面已进入 OAuth consent 时误点授权按钮。

    used_codes: set[str] = set()
    max_otp_attempts = 3

    def _restart_email_otp_flow(reason: str) -> None:
        """Codex Auth 上直接点 resend 可能触发服务端 500；这里改为重新打开授权地址并提交邮箱。"""
        nonlocal otp_after_ts
        logger.info("[Codex][Browser] 重新触发邮箱 OTP：%s", reason)
        otp_after_ts = time.time()
        driver.get(auth_url)
        human_delay("navigate")
        _maybe_accept(driver)
        try:
            _type_email_address(driver, email, timeout=12)
            human_delay("form")
            _submit_email_step(driver)
            logger.info("[Codex][Browser] 已重新提交邮箱触发 OTP")
            _maybe_click_passwordless_after_email(driver, email, timeout=12)
        except Exception as exc:
            # 如果重进授权地址后已经停在验证码/下一步页面，就不要再强行提交。
            if not _is_email_verification_page(driver):
                logger.warning("[Codex][Browser] 重新提交邮箱失败，继续按当前页面轮询：%s", str(exc)[:180])
            else:
                logger.info("[Codex][Browser] 重开授权后已在邮箱 OTP 页面")
        human_delay("api")

    for otp_attempt in range(1, max_otp_attempts + 1):
        logger.info("[Codex][Browser] 等待邮箱 OTP：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
        try:
            code = _wait_for_fresh_email_otp(
                otp_provider,
                email,
                after_ts=otp_after_ts,
                used_codes=used_codes,
                timeout=90,
            )
        except Exception as exc:
            if otp_attempt >= max_otp_attempts:
                raise
            logger.warning(
                "[Codex][Browser] 一直未收到邮箱 OTP，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                otp_attempt + 1,
                max_otp_attempts,
                type(exc).__name__,
                str(exc)[:180],
            )
            _restart_email_otp_flow("等待验证码超时，避免点击 resend 导致 500")
            continue
        used_codes.add(str(code))
        logger.info("[Codex][Browser] 邮箱 OTP 收到：%s", code)
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        logger.info("[Codex][Browser] 已填写邮箱 OTP")
        human_delay("otp_input")
        _install_email_otp_validate_hook(driver)
        clicked = _click_if_present(driver, [
            "button[type='submit']",
            "//button[contains(., 'Continue')]",
            "//button[contains(., '继续')]",
            "//button[contains(., 'Verify')]",
            "//button[contains(., '验证')]",
        ], timeout=8)
        if clicked:
            logger.info("[Codex][Browser] 已提交邮箱 OTP，等待后续授权/手机号页面")
        else:
            logger.info("[Codex][Browser] 未找到显式提交按钮，继续等待页面状态")

        outcome = _wait_after_email_otp_submit(driver, timeout=45)
        logger.info("[Codex][Browser] 邮箱 OTP 提交后状态：%s", outcome)
        if outcome == "accepted":
            return
        if str(outcome).startswith("deactivated:"):
            error_code = str(outcome).split(":", 1)[1] or "account_deactivated"
            raise AccountUnusableError(f"账号已废（{error_code}）", error_code=error_code)

        if otp_attempt >= max_otp_attempts:
            raise RuntimeError("Codex 邮箱验证码连续错误/过期，已达到最大重试次数")

        logger.warning(
            "[Codex][Browser] 邮箱验证码错误/过期或页面未跳转，准备重新发送并重新获取最新验证码（%s/%s）",
            otp_attempt + 1,
            max_otp_attempts,
        )
        _restart_email_otp_flow("验证码错误/过期或页面未跳转，避免点击 resend 导致 500")



def _wait_for_fresh_email_otp(otp_provider, email: str, after_ts: float, used_codes: set[str] | None = None, timeout: int = 90) -> str:
    """获取一个未提交过的邮箱 OTP。

    通用 API 邮箱的取码接口有时会先返回缓存旧码；验证码错误后重发时，
    这里会拒绝复用已失败的 code，持续轮询直到出现新 code 或超时。
    """
    used_codes = {str(x) for x in (used_codes or set()) if x}
    end = time.time() + timeout
    last_code = ""
    while True:
        code = str(otp_provider(email, after_ts=after_ts) or "").strip()
        if code and code not in used_codes:
            return code
        last_code = code or last_code
        remaining = int(end - time.time())
        if remaining <= 0:
            raise RuntimeError(f"等待新的邮箱验证码超时，取码接口仍返回已失败验证码：{last_code or '-'}")
        logger.warning(
            "[Codex][Browser] 取码接口仍返回已提交过的旧 OTP=%s，继续等待最新验证码（剩余 %ss）",
            last_code or "-",
            remaining,
        )
        time.sleep(min(5, max(1, remaining)))


def _install_email_otp_validate_hook(driver) -> None:
    """
    在页面内 hook fetch/XHR，捕获 email-otp/validate 的接口响应体。

    指纹浏览器不能像纯协议模式一样直接拿 requests.Response，因此在提交邮箱 OTP 前
    注入此 hook，后续只读取接口 JSON error.code，不靠页面文字判断废号。
    """
    script = r"""
    (() => {
      window.__codexEmailOtpValidateResponses = [];
      if (window.__codexEmailOtpValidateHooked) return true;
      window.__codexEmailOtpValidateHooked = true;
      const hit = (url) => String(url || '').includes('/api/accounts/email-otp/validate');
      const save = (url, status, body) => {
        try {
          if (!hit(url)) return;
          window.__codexEmailOtpValidateResponses.push({
            url: String(url || ''),
            status: Number(status || 0),
            body: String(body || '').slice(0, 2000),
            ts: Date.now(),
          });
        } catch (e) {}
      };
      const origFetch = window.fetch;
      if (origFetch) {
        window.fetch = async function(input, init) {
          const resp = await origFetch.apply(this, arguments);
          try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            if (hit(url)) {
              resp.clone().text().then(t => save(url, resp.status, t)).catch(() => {});
            }
          } catch (e) {}
          return resp;
        };
      }
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        this.__codexOtpValidateUrl = url;
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        try {
          this.addEventListener('loadend', function() {
            try {
              if (hit(this.__codexOtpValidateUrl)) save(this.__codexOtpValidateUrl, this.status, this.responseText);
            } catch (e) {}
          });
        } catch (e) {}
        return origSend.apply(this, arguments);
      };
      return true;
    })();
    """
    try:
        driver.execute_script(script)
    except Exception as exc:
        logger.debug("[Codex][Browser] 注入 email-otp/validate 响应 hook 失败：%s", exc)


def _read_email_otp_validate_dead_code(driver) -> str:
    try:
        rows = driver.execute_script("return window.__codexEmailOtpValidateResponses || [];") or []
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        code = detect_account_unusable_response_body(str(row.get("body") or ""))
        if code:
            logger.warning(
                "[Codex][Browser] email-otp/validate 响应识别账号已废：code=%s status=%s",
                code,
                row.get("status"),
            )
            return code
    return ""


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or "").lower()
        return "email-verification" in url
    except Exception:
        return False


def _wait_after_email_otp_submit(driver, timeout: int = 45) -> str:
    """
    提交邮箱 OTP 后等待页面离开 /email-verification。

    返回：
      - accepted：已离开邮箱验证码页 / 进入手机号页 / 进入 callback；
      - invalid：页面明确报错、输入框标红，或长时间停留验证码页。
    """
    end = time.time() + timeout
    last_url = ""
    last_log = 0.0
    while time.time() < end:
        try:
            dead_code = _read_email_otp_validate_dead_code(driver)
            if dead_code:
                return f"deactivated:{dead_code}"
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] 邮箱 OTP 后等待跳转：url=%s", url)
                last_url = url
            if _is_callback_url(url):
                return "accepted"
            if _has_strict_add_phone_form(driver) or _is_phone_code_page(driver):
                return "accepted"
            # 已经离开 email-verification，交给后续授权/手机号/consent 流程处理。
            if "email-verification" not in url.lower():
                return "accepted"

            state = _email_otp_page_state(driver)
            invalid = any(str(i.get("ariaInvalid") or "").lower() == "true" for i in (state.get("inputs") or []))
            errors = [str(x) for x in (state.get("errors") or []) if str(x).strip()]
            body_text = str(state.get("text") or "").lower()
            error_hit = any(x in body_text for x in (
                "invalid code", "incorrect code", "wrong code", "expired",
                "验证码错误", "验证码无效", "验证码已过期", "コードが正しく", "無効", "期限",
            ))
            if invalid or errors or error_hit:
                logger.warning(
                    "[Codex][Browser] 邮箱 OTP 提交后检测到错误/仍需验证码：errors=%s invalid=%s url=%s",
                    errors[:3],
                    invalid,
                    url,
                )
                return "invalid"

            if time.time() - last_log > 6:
                logger.info("[Codex][Browser] 邮箱 OTP 后仍在 email-verification，继续等待页面自动跳转")
                last_log = time.time()
        except Exception:
            pass
        time.sleep(0.5)
    logger.warning("[Codex][Browser] 邮箱 OTP 后等待跳转超时，当前 url=%s，按验证码无效/过期处理", getattr(driver, "current_url", ""))
    return "invalid"


def _phone_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible).map(el => ({
          name: el.name || '', value: el.value || '', checked: !!el.checked, id: el.id || ''
        }));
        const inputs = [...document.querySelectorAll('input,select,textarea')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', placeholder: el.getAttribute('placeholder') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const bodyText = (document.body?.innerText || '').slice(0, 1200);
        return {url: location.href, radios, inputs, forms, bodyText};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _select_sms_channel_or_raise(driver) -> None:
    state = _phone_page_state(driver)
    radios = state.get('radios') or []
    # 如果存在 WhatsApp 且没有 SMS/text 可选，当前接码平台无法读取 WhatsApp，直接换号。
    has_whatsapp = any('whatsapp' in str(r.get('value','')).lower().replace(' ', '') for r in radios)
    has_sms = any(str(r.get('value','')).lower() in ('sms', 'text', 'text_message', 'text-message') for r in radios)
    if has_whatsapp and not has_sms:
        raise RuntimeError(f"whatsapp_channel: 页面仅提供 WhatsApp 通道 state={state}")
    if any(
        str(r.get('value', '')).lower() in ('sms', 'text', 'text_message', 'text-message')
        and r.get('checked')
        for r in radios
    ):
        logger.info("[Codex][Browser] SMS 短信通道已经选中，不重复触发")
        return
    # 选择 SMS/text radio。无 radio 时可能默认 SMS。
    selected = driver.execute_script(r"""
    const radios = [...document.querySelectorAll('input[type=radio]')];
    const sms = radios.find(el => /^(sms|text|text_message|text-message)$/i.test(el.value || ''));
    if (!sms) return false;
    sms.click();
    sms.dispatchEvent(new Event('input', {bubbles:true}));
    sms.dispatchEvent(new Event('change', {bubbles:true}));
    return true;
    """)
    if selected:
        logger.info("[Codex][Browser] 已选择 SMS 短信通道")


def _is_phone_code_state(state: dict) -> bool:
    url = str(state.get('url') or '').lower()
    if 'email-verification' in url:
        # 邮箱 OTP 页面也会出现 autocomplete=one-time-code，不能误判成手机验证码页。
        return False
    if 'phone-verification' in url:
        return True
    forms = state.get('forms') or []
    form_actions = ' '.join(str(f.get('action') or '') for f in forms).lower()
    if 'phone-verification' in form_actions:
        return True
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','placeholder')) for i in inputs).lower()
    body = str(state.get('bodyText') or '').lower()
    has_code_input = 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs
    phone_hint = (
        'phone' in url or 'phone' in form_actions
        or 'check your phone' in body
        or 'verification code we just sent' in body
        or 'enter the verification code' in body and ('text message' in body or 'phone' in body)
        or 'resend text message' in body
        or 'sent to +' in body
    )
    return bool(phone_hint and has_code_input)


def _is_phone_code_page(driver) -> bool:
    return _is_phone_code_state(_phone_page_state(driver))


def _is_add_phone_page(driver) -> bool:
    state = _phone_page_state(driver)
    url = str(state.get('url') or '').lower()
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete')) for i in inputs).lower()
    return 'add-phone' in url or 'type tel' in attrs or 'phone' in attrs or 'tel' in attrs


_PHONE_INPUT_SELECTORS = [
    "input[type='tel']",
    "input[name='phone']",
    "input[name='phone_number']",
    "input[autocomplete='tel']",
    "input[id*='phone']",
    "input[placeholder*='Phone']",
    "input[placeholder*='phone']",
]


_PHONE_COUNTRY_HINTS = {
    "212": {"iso2": "MA", "names": ["Morocco", "摩洛哥"]},
    "351": {"iso2": "PT", "names": ["Portugal", "葡萄牙"]},
    "27": {"iso2": "ZA", "names": ["South Africa", "南非"]},
    "31": {"iso2": "NL", "names": ["Netherlands", "荷兰"]},
    "34": {"iso2": "ES", "names": ["Spain", "西班牙"]},
    "43": {"iso2": "AT", "names": ["Austria", "奥地利"]},
    "44": {"iso2": "GB", "names": ["United Kingdom", "UK", "英国"]},
    "54": {"iso2": "AR", "names": ["Argentina", "阿根廷"]},
    "55": {"iso2": "BR", "names": ["Brazil", "巴西"]},
    "57": {"iso2": "CO", "names": ["Colombia", "哥伦比亚"]},
    "62": {"iso2": "ID", "names": ["Indonesia", "印度尼西亚"]},
    "63": {"iso2": "PH", "names": ["Philippines", "菲律宾"]},
}


def _phone_country_selection_hints(phone: str) -> dict:
    """根据 E.164 前缀返回 OpenAI 国家下拉框的匹配提示。"""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    for dial_code in sorted(_PHONE_COUNTRY_HINTS, key=len, reverse=True):
        if digits.startswith(dial_code):
            item = _PHONE_COUNTRY_HINTS[dial_code]
            return {
                "dial_code": dial_code,
                "iso2": str(item.get("iso2") or ""),
                "names": list(item.get("names") or []),
            }
    return {"dial_code": "", "iso2": "", "names": []}


def _phone_values_match(expected_e164: str, visible_value: str, hidden_value: str) -> bool:
    """接受完整 E.164 可见值，也接受“国家已选中 + 可见框为本地号码”。"""
    digits = lambda value: "".join(ch for ch in str(value or "") if ch.isdigit())
    expected_digits = digits(expected_e164)
    visible_digits = digits(visible_value)
    hidden_digits = digits(hidden_value)
    if not expected_digits or not visible_digits:
        return False
    if hidden_value and hidden_digits != expected_digits:
        return False
    if visible_digits == expected_digits:
        return True
    return bool(
        hidden_digits == expected_digits
        and len(visible_digits) >= 7
        and expected_digits.endswith(visible_digits)
    )


def _type_phone_input_element(phone_input, visible_value: str) -> None:
    """Use WebDriver keystrokes so React receives trusted keyboard/input events."""
    from selenium.webdriver.common.keys import Keys

    phone_input.click()
    phone_input.send_keys(Keys.CONTROL, "a")
    phone_input.send_keys(Keys.BACKSPACE)
    phone_input.send_keys(str(visible_value or ""))


def _has_strict_add_phone_form(driver) -> bool:
    try:
        return bool(driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return false;
        return !![...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
        """))
    except Exception:
        return False


def _auth_origin(driver) -> str:
    try:
        parsed = urlparse(str(driver.current_url or ""))
        if parsed.scheme and parsed.netloc and parsed.hostname and parsed.hostname.endswith("openai.com"):
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return "https://auth.openai.com"


def _ensure_add_phone_input(driver, *, reason: str = ""):
    """确保当前页面回到 add-phone，并返回手机号输入框。

    换号时如果还停留在 phone-verification/OTP 页，必须先回到手机号页，
    再把新号码重新写入页面并重新提交。
    """
    if _has_strict_add_phone_form(driver):
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)

    current = str(getattr(driver, "current_url", "") or "")
    if "email-verification" in current.lower():
        logger.info("[Codex][Browser] 当前仍在 email-verification，先等待授权流程自动跳转，避免 invalid_auth_step")
        _wait_after_email_otp_submit(driver, timeout=45)
        if _has_strict_add_phone_form(driver):
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
        current = str(getattr(driver, "current_url", "") or "")

    target = _auth_origin(driver).rstrip("/") + "/add-phone"
    logger.info(
        "[Codex][Browser] 当前不在手机号输入页，准备重新打开 add-phone 后换号：reason=%s url=%s target=%s",
        reason or "retry", current, target,
    )
    try:
        driver.get(target)
        human_delay("navigate")
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=10)
    except Exception as first_exc:
        # 某些流程不允许直接打开 /add-phone，尝试浏览器返回到上一页。
        logger.info("[Codex][Browser] 直接打开 add-phone 未拿到输入框，尝试 history back：%s", str(first_exc)[:160])
        try:
            driver.back()
            human_delay("navigate")
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
        except Exception as back_exc:
            raise RuntimeError(
                f"无法回到手机号输入页以重新换号: direct={type(first_exc).__name__}: {first_exc}; "
                f"back={type(back_exc).__name__}: {back_exc}; state={_phone_page_state(driver)}"
            )


def _set_phone_value(driver, phone: str, *, timeout: int = 10) -> dict:
    """按 FlowPilot 第 9 步逻辑填写 add-phone 表单。

    要点：
    - 所有元素 scoped 到 form[action*="/add-phone"]；
    - 可见 tel 输入框写入“页面期望显示的号码”；
    - 如果页面存在隐藏 input[name="phoneNumber"]，同步写入完整 E.164 号码；
    - 触发 input/change 并 blur，让 React/React-Aria 完成校验。
    """
    if not _has_strict_add_phone_form(driver):
        raise RuntimeError(f"当前不是 add-phone 手机号输入页，不能填写手机号: state={_phone_page_state(driver)}")
    country_hints = _phone_country_selection_hints(phone)
    result = driver.execute_script(r"""
    const rawPhone = String(arguments[0] || '').trim();
    const countryHints = arguments[1] || {};
    const hintedDialCode = String(countryHints.dial_code || '').replace(/\D+/g, '');
    const hintedIso2 = String(countryHints.iso2 || '').trim().toLowerCase();
    const hintedNames = Array.isArray(countryHints.names)
      ? countryHints.names.map(x => String(x || '').trim().toLowerCase()).filter(Boolean)
      : [];
    const e164 = rawPhone.startsWith('+') ? rawPhone : ('+' + rawPhone.replace(/\D+/g, ''));
    const digits = e164.replace(/\D+/g, '');
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
    if (!form) {
      return {ok:false, error:'missing_add_phone_form', url: location.href};
    }
    const phoneInput = [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')]
      .find(visible);
    if (!phoneInput) {
      return {ok:false, error:'missing_phone_input', url: location.href};
    }

    const hiddenPhoneNumberInput = form.querySelector('input[name="phoneNumber"]');
    const select = form.querySelector('select');
    let dialCode = '';
    let selectedText = '';
    let selectedChanged = false;
    const optionDialCode = (opt) => {
      const text = String(opt?.textContent || opt?.label || opt?.value || '').replace(/\s+/g, ' ').trim();
      const m = text.match(/\+(\d{1,4})\b/);
      return m ? m[1] : '';
    };
    if (select) {
      // 参考 FlowPilot ensureCountrySelected：按号码前缀选择对应国家/区号，避免默认国家与号码不一致。
      const options = [...select.options];
      const hintedMatch = options.find(opt => {
        const fields = [
          opt.value,
          opt.label,
          opt.textContent,
          opt.getAttribute('data-country-code'),
          opt.getAttribute('data-code'),
          opt.getAttribute('data-key'),
        ].map(x => String(x || '').replace(/\s+/g, ' ').trim().toLowerCase());
        const tokens = fields.flatMap(x => x.split(/[^a-z0-9]+/).filter(Boolean));
        if (hintedIso2 && tokens.includes(hintedIso2)) return true;
        return hintedNames.some(name => fields.some(field => field === name || field.includes(name)));
      });
      const prefixMatch = options
        .map(opt => ({opt, code: optionDialCode(opt)}))
        .filter(x => x.code && digits.startsWith(x.code))
        .sort((a, b) => b.code.length - a.code.length)[0];
      const matched = hintedMatch
        ? {opt: hintedMatch, code: optionDialCode(hintedMatch) || hintedDialCode}
        : prefixMatch;
      if (matched && select.value !== matched.opt.value) {
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
        if (setter) setter.call(select, matched.opt.value); else select.value = matched.opt.value;
        select.dispatchEvent(new Event('input', {bubbles:true}));
        select.dispatchEvent(new Event('change', {bubbles:true}));
        selectedChanged = true;
      }
      if (select.selectedIndex >= 0 && select.options[select.selectedIndex]) {
        const opt = select.options[select.selectedIndex];
        selectedText = String(opt.textContent || opt.label || opt.value || '').replace(/\s+/g, ' ').trim();
        dialCode = optionDialCode(opt) || (matched && matched.opt === opt ? hintedDialCode : '');
      }
    }

    // FlowPilot：可见框一般填 national number；隐藏 phoneNumber 填完整 E.164。
    // 若无法判断页面区号，则可见框填完整 +E164，避免丢国家码。
    let visibleValue = e164;
    if (dialCode && digits.startsWith(dialCode) && digits.length > dialCode.length + 3) {
      visibleValue = digits.slice(dialCode.length);
      if (!visibleValue) visibleValue = e164;
    }

    phoneInput.scrollIntoView({block:'center'});
    return {
      ok: true,
      e164,
      visibleValue,
      actualVisible: phoneInput.value || '',
      hiddenValue: hiddenPhoneNumberInput ? (hiddenPhoneNumberInput.value || '') : '',
      dialCode,
      selectedText,
      selectedChanged,
      inputName: phoneInput.getAttribute('name') || '',
      inputId: phoneInput.id || '',
      url: location.href,
    };
    """, phone, country_hints)
    if not result or not result.get("ok"):
        raise RuntimeError(f"手机号写入失败 result={result} state={_phone_page_state(driver)}")
    phone_input = _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=timeout)
    _type_phone_input_element(phone_input, str(result.get("visibleValue") or ""))
    # Let the phone component format the visible value and derive its hidden
    # E.164 field from real keyboard events instead of mutating DOM-only state.
    settle_deadline = time.time() + 3
    while time.time() < settle_deadline:
        settled = driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        const input = form && [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
        const hidden = form && form.querySelector('input[name="phoneNumber"]');
        return {
          actualVisible: String(input?.value || ''),
          hiddenValue: String(hidden?.value || ''),
          ariaInvalid: String(input?.getAttribute('aria-invalid') || ''),
        };
        """) or {}
        result.update(settled)
        if _phone_values_match(
            str(result.get("e164") or ""),
            str(result.get("actualVisible") or ""),
            str(result.get("hiddenValue") or ""),
        ):
            break
        time.sleep(0.1)
    try:
        phone_input.send_keys("\ue004")  # TAB: commit React-Aria field state via a real blur.
    except Exception:
        pass
    actual = str(result.get("actualVisible") or "").strip()
    visible_value = str(result.get("visibleValue") or "").strip()
    hidden_value = str(result.get("hiddenValue") or "").strip()
    e164 = str(result.get("e164") or "").strip()
    # OpenAI/React-Aria 电话框会自动格式化，例如 +84925154291 -> +84 925 154 291。
    # 不能按界面字符串精确比较，只比较数字归一化后的值。
    actual_digits = ''.join(ch for ch in actual if ch.isdigit())
    visible_digits = ''.join(ch for ch in visible_value if ch.isdigit())
    e164_digits = ''.join(ch for ch in e164 if ch.isdigit())
    hidden_digits = ''.join(ch for ch in hidden_value if ch.isdigit())
    expected_visible_ok = bool(actual_digits) and (actual_digits == visible_digits or actual_digits == e164_digits)
    if not expected_visible_ok:
        raise RuntimeError(f"手机号可见输入框校验失败 expected_digits={visible_digits or e164_digits} actual={actual} result={result} state={_phone_page_state(driver)}")
    if hidden_value and hidden_digits != e164_digits:
        raise RuntimeError(f"手机号隐藏字段校验失败 expected={e164} actual={hidden_value} result={result} state={_phone_page_state(driver)}")
    return result


def _blur_active_input_and_wait(driver, *, label: str = "输入完成") -> None:
    """输入手机号后移开焦点，并给前端校验/格式化留处理时间。"""
    try:
        driver.execute_script(r"""
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') active.blur();
        document.body?.focus?.();
        document.dispatchEvent(new Event('change', {bubbles:true}));
        """)
    except Exception:
        pass
    seconds = random.uniform(1.8, 3.2)
    logger.info("[Codex][Browser] %s，已移开焦点，等待页面处理 %.1f 秒", label, seconds)
    time.sleep(seconds)


def _verify_add_phone_value_before_submit(driver, expected_e164: str) -> dict:
    result = driver.execute_script(r"""
    const expected = String(arguments[0] || '').trim();
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
    if (!form) return {ok:false, error:'missing_add_phone_form', url: location.href};
    const input = [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
    const hidden = form.querySelector('input[name="phoneNumber"]');
    const visibleValue = String(input?.value || '').trim();
    const hiddenValue = String(hidden?.value || '').trim();
    const digits = value => String(value || '').replace(/\D+/g, '');
    const visibleDigits = digits(visibleValue);
    const hiddenDigits = digits(hiddenValue);
    const expectedDigits = digits(expected);
    // 输入框可能被自动格式化，按数字比较；隐藏字段如果存在必须等于完整 E.164。
    const ok = !!visibleDigits && visibleDigits === expectedDigits && (!hidden || hiddenDigits === expectedDigits);
    return {ok, visibleValue, hiddenValue, expected, visibleDigits, hiddenDigits, expectedDigits, url: location.href};
    """, expected_e164)
    if not result or not _phone_values_match(
        expected_e164,
        str(result.get("visibleValue") or ""),
        str(result.get("hiddenValue") or ""),
    ):
        raise RuntimeError(f"手机号提交前校验失败 result={result} state={_phone_page_state(driver)}")
    result["ok"] = True
    return result


def _wait_page_settle_after_submit() -> None:
    """点击提交后先等待页面处理，再检查发送状态。"""
    seconds = random.uniform(2.0, 4.0)
    logger.info("[Codex][Browser] 已点击提交，等待页面发送/跳转处理 %.1f 秒后检查状态", seconds)
    time.sleep(seconds)


def _refresh_add_phone_for_retry(driver, *, reason: str = "") -> None:
    """发送失败/换号前刷新手机号页，避免旧错误状态和旧号码残留。"""
    try:
        logger.info("[Codex][Browser] 发送失败/准备换号，刷新手机号页面：%s", reason or "retry")
        driver.refresh()
        human_delay("navigate")
        try:
            _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
            return
        except Exception:
            pass
        # 如果刷新后仍不在输入页，强制回 add-phone。
        target = _auth_origin(driver).rstrip("/") + "/add-phone"
        logger.info("[Codex][Browser] 刷新后未找到手机号输入框，重新打开：%s", target)
        driver.get(target)
        human_delay("navigate")
        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
    except Exception as exc:
        logger.info("[Codex][Browser] 刷新手机号页失败，下一轮会再次尝试回到 add-phone：%s", str(exc)[:180])


def _click_add_phone_continue_button(driver, *, timeout: int = 10) -> dict:
    """点击 add-phone 表单里的 Continue/続行 按钮。

    参考 FlowPilot 的 getAddPhoneSubmitButton + simulateClick：优先在 add-phone form 内找
    enabled submit，点击失败时用 form.requestSubmit(button) 兜底。
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => {
              if (!el) return false;
              if (el.disabled) return false;
              if (String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
              return true;
            };
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            if (!form) return null;
            const buttons = [...form.querySelectorAll('button[type="submit"], input[type="submit"]')];
            return buttons.find(b => visible(b) && enabled(b) && (b.getAttribute('data-dd-action-name') || '').toLowerCase() === 'continue')
              || buttons.find(b => visible(b) && enabled(b))
              || buttons.find(b => visible(b))
              || null;
            """)
            if btn:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(random.uniform(0.3, 0.8))
                try:
                    text = str(getattr(btn, 'text', '') or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                except Exception:
                    text = ''
                try:
                    btn.click()
                    _wait_page_settle_after_submit()
                    return {"ok": True, "method": "click", "text": text}
                except Exception as click_exc:
                    last = click_exc
                    submitted = driver.execute_script(r"""
                    const btn = arguments[0];
                    const form = btn?.form || btn?.closest?.('form');
                    if (form && typeof form.requestSubmit === 'function') {
                      form.requestSubmit(btn);
                      return true;
                    }
                    if (btn && typeof btn.click === 'function') {
                      btn.click();
                      return true;
                    }
                    return false;
                    """, btn)
                    if submitted:
                        _wait_page_settle_after_submit()
                        return {"ok": True, "method": "requestSubmit", "text": text, "click_error": str(click_exc)[:160]}
        except Exception as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"submit_missing: add-phone Continue/続行 submit button not found last={last} state={_phone_page_state(driver)}")


def _force_submit_add_phone_form(driver) -> dict:
    """add-phone 页面点击按钮没生效时，直接 requestSubmit 当前 form。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return {ok:false, reason:'missing_form', url: location.href};
        const btn = [...form.querySelectorAll('button[type="submit"],input[type="submit"]')]
          .find(el => visible(el) && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          || form.querySelector('button[type="submit"],input[type="submit"]');
        if (btn) btn.scrollIntoView({block:'center'});
        if (typeof form.requestSubmit === 'function') form.requestSubmit(btn || undefined);
        else if (btn && typeof btn.click === 'function') btn.click();
        else form.submit();
        return {ok:true, method: btn ? 'requestSubmit(button)' : 'requestSubmit(form)', url: location.href};
        """) or {}
    except Exception as exc:
        return {ok:false, reason:f'{type(exc).__name__}: {exc}', url:getattr(driver, 'current_url', '')}


def _wait_after_phone_send(driver, timeout: int = 12) -> str:
    end = time.time() + timeout
    last = {}
    force_submitted = False
    while time.time() < end:
        time.sleep(1)
        last = _phone_page_state(driver)
        # 必须优先判断验证码页：页面文案里可能包含 send/limit/check 等词，不能把
        # “Check your phone / Enter the verification code...” 误判成发送失败。
        if _is_phone_code_state(last):
            return 'code_page'
        body = str(last.get('bodyText') or '')
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {body[:240]}")
        # 仍在 add-phone 且字段有 aria-invalid，认为号码被拒。
        if _is_add_phone_page(driver):
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
            if invalid:
                raise RuntimeError(f"invalid_phone: add-phone input aria-invalid state={last}")
            # Cloak/React-Aria 场景下 btn.click 可能只聚焦没触发表单提交；补一次 requestSubmit。
            if not force_submitted and time.time() > end - timeout + 3:
                info = _force_submit_add_phone_form(driver)
                logger.info("[Codex][Browser] add-phone 点击后仍停留本页，补执行 form.requestSubmit：%s", info)
                force_submitted = True
                time.sleep(2)
    if _is_phone_code_state(last) or _is_phone_code_page(driver):
        return 'code_page'
    if _is_add_phone_page(driver):
        raise RuntimeError(f"send_not_accepted: 提交后仍停留在 add-phone state={last}")
    return 'unknown'


def _wait_after_phone_otp_submit(driver, timeout: int = 20) -> str:
    """手机验证码提交后等待结果。

    成功时通常会跳出 phone-verification，进入 consent/workspace/callback；不能在提交后
    3 秒立刻读取旧页面文案并按 send_limited 判失败。只有明确仍在手机号流程且出现错误时
    才返回失败。
    """
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(1)
        current = str(getattr(driver, "current_url", "") or "")
        if _is_callback_url(current):
            return "callback"
        last = _phone_page_state(driver)
        # 已离开手机验证码/加手机号页面，说明验证码被接受，后续交给 consent/callback 流程。
        if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
            return "left_phone_flow"
        # 仍在验证码页时，只把明确错误当失败；普通 Check your phone 页面继续等。
        if _is_phone_code_state(last):
            inputs = last.get('inputs') or []
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in inputs)
            body = str(last.get('bodyText') or '').lower()
            if invalid or any(k in body for k in (
                'invalid code', 'incorrect code', 'wrong code', 'expired code',
                'code is invalid', 'code was invalid', '验证码无效', '验证码错误', '验证码已过期',
                '認証コードが無効', 'コードが正しく',
            )):
                raise RuntimeError(f"invalid_phone_code: {(last.get('bodyText') or '')[:240]}")
            continue
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {(last.get('bodyText') or '')[:240]}")
    # 超时后再看一次：如果已经离开手机号流程，视为通过；如果仍在验证码页但没明确错误，交给后续流程继续试。
    current = str(getattr(driver, "current_url", "") or "")
    if _is_callback_url(current):
        return "callback"
    last = _phone_page_state(driver)
    if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
        return "left_phone_flow"
    if _is_phone_code_state(last):
        return "still_code_page"
    return "unknown"


def _classify_phone_page_failure(state: dict) -> str:
    if _is_phone_code_state(state):
        return ''
    radios = state.get('radios') or []
    text = str(state.get('bodyText') or '').lower()
    if any(k in text for k in (
        'phone number required', 'phone number is required', 'add a phone number',
        '电话号码必填', '手机号必填', '请添加电话号码',
        '전화번호 필수', '전화번호를 추가',
    )):
        return 'phone_required'
    # WhatsApp 用 DOM radio value 判断；其它发送失败用服务端/页面错误文本兜底。
    if any('whatsapp' in str(r.get('value','')).lower().replace(' ', '') and r.get('checked') for r in radios):
        return 'whatsapp_channel'
    if 'invalid_auth_step' in text or 'invalid auth step' in text:
        return 'invalid_auth_step'
    has_sms_radio = any(str(r.get('value','')).lower() in ('sms', 'text', 'text_message', 'text-message') for r in radios)
    if ('whatsapp' in text or 'whats app' in text) and not has_sms_radio:
        return 'whatsapp_channel'
    if any(k in text for k in ('invalid phone', 'not a valid phone', 'phone number is not valid', '号码无效', '手机号无效')):
        return 'invalid_phone'
    if any(k in text for k in (
        'cannot send', 'could not send', 'unable to send', 'failed to send', 'send failed',
        '发送失败', '发送失败了', '无法发送', '不能发送', '无法向',
        '送信できません', '送信に失敗', '送信できなかった',
    )):
        return 'delivery_refused'
    if any(k in text for k in ('too many', 'rate limit', 'throttle', '频繁', '限流')):
        return 'send_limited'
    return ''

def _sleep_before_phone_retry(attempt: int, max_retries: int, *, prefix: str = "[Codex][Browser]") -> None:
    """换号前随机等待，至少 3 秒，避免连续提交号码过快。"""
    if attempt >= max_retries:
        return
    seconds = random.uniform(3.0, 8.0)
    logger.info("%s 换号前随机等待 %.1f 秒", prefix, seconds)
    time.sleep(seconds)


def _prepare_phone_submission(driver, phone_e164: str) -> tuple[dict, dict]:
    """Select the delivery channel first, then type and validate the phone number."""
    logger.info("[Codex][Browser] 检查并选择 SMS 短信通道")
    _select_sms_channel_or_raise(driver)
    _blur_active_input_and_wait(driver, label="短信通道确认完成")

    phone_fill = _set_phone_value(driver, phone_e164, timeout=10)
    logger.info(
        "[Codex][Browser] 已重新设置手机号：e164=%s visible=%s hidden=%s dialCode=%s country=%s",
        phone_fill.get("e164"), phone_fill.get("actualVisible"), phone_fill.get("hiddenValue") or "-",
        phone_fill.get("dialCode") or "-", (str(phone_fill.get("selectedText") or "-") + (" [changed]" if phone_fill.get("selectedChanged") else "")),
    )
    _blur_active_input_and_wait(driver, label="手机号输入完成")
    phone_verify = _verify_add_phone_value_before_submit(driver, str(phone_fill.get("e164") or phone_e164))
    logger.info("[Codex][Browser] 手机号提交前校验通过：visible=%s hidden=%s", phone_verify.get("visibleValue"), phone_verify.get("hiddenValue") or "-")
    return phone_fill, phone_verify


def _build_sms_country_selector() -> PreferredCountrySelector | None:
    """Build per-registration country routing from the hot-loaded SMS config."""
    if sms_provider._provider() not in {"grizzly", "sms_activate"}:
        return None
    cfg = sms_provider._cfg
    return PreferredCountrySelector(
        getattr(cfg, "SMS_PREFERRED_COUNTRIES", []),
        fallback_country=getattr(cfg, "SMS_COUNTRY", ""),
        failure_switch=getattr(cfg, "SMS_COUNTRY_FAILURE_SWITCH", 2),
        max_price=getattr(cfg, "SMS_MAX_PRICE", ""),
    )


def _choose_sms_country(selector, http, force: bool = False) -> str | None:
    if selector is None:
        return None
    try:
        offers = sms_provider.get_country_offers(
            selector.preferred_countries,
            service=sms_provider._cfg.SMS_SERVICE,
            http=http,
            force=force,
        )
    except sms_provider.SmsNoBalanceError:
        raise
    except sms_provider.SmsProviderError as exc:
        logger.warning(
            "[Codex][Browser] SMS 国家报价不可用，按保存顺序选择：%s: %s",
            type(exc).__name__,
            str(exc)[:180],
        )
        country = None
        try:
            country = selector.choose(None, allow_order_fallback=True)
            return country
        finally:
            logger.info(
                "[Codex][Browser] SMS 国家选择 country=%s reason=%s offers=%s",
                country or "-",
                selector.last_reason,
                "fallback",
            )

    summary = ",".join(
        f"{item.country_code}={item.price}/{item.available_count}" for item in offers
    ) or "-"
    country = None
    try:
        country = selector.choose(offers)
        return country
    finally:
        logger.info(
            "[Codex][Browser] SMS 国家选择 country=%s reason=%s offers=%s",
            country or "-",
            selector.last_reason,
            summary,
        )


def _do_phone_verification_if_present(driver) -> None:
    """如果页面要求手机号验证，则用当前 sms_provider 自动完成。"""
    provider = sms_provider._provider()
    http = sms_provider._http()
    max_retries = int(getattr(sms_provider._cfg, "SMS_MAX_RETRIES", 10) or 10) if hasattr(sms_provider, "_cfg") else 10
    try:
        # 如果页面没有手机号输入框，直接返回。
        try:
            end_detect = time.time() + 8
            while time.time() < end_detect and not _has_strict_add_phone_form(driver):
                # 如果已经在验证码页，说明手机步骤之前已提交过；继续处理验证码页，不应当跳过。
                if _is_phone_code_page(driver):
                    break
                time.sleep(0.5)
            if not (_has_strict_add_phone_form(driver) or _is_phone_code_page(driver)):
                raise RuntimeError("not_phone_flow")
        except Exception:
            logger.info("[Codex][Browser] 未检测到手机号验证页，跳过手机步骤")
            return

        selector = _build_sms_country_selector()
        last_err = None
        actual_attempt = 0
        fixed_no_numbers_attempt = 0
        while actual_attempt < max_retries:
            country = _choose_sms_country(
                selector,
                http,
                force=bool(selector and selector.needs_offer_refresh),
            )
            activation_id = None
            try:
                activation_id, phone = sms_provider.acquire_number(http, country=country)
                actual_attempt += 1
                logger.info("[Codex][Browser] 手机验证尝试 %s/%s，provider=%s，号码=+%s", actual_attempt, max_retries, provider, phone)
                logger.info("[Codex][Browser] 准备手机号输入页，重新设置新手机号")
                _ensure_add_phone_input(driver, reason=f"attempt-{actual_attempt}")
                _prepare_phone_submission(driver, f"+{phone}")
                submit_info = _click_add_phone_continue_button(driver, timeout=10)
                logger.info("[Codex][Browser] 已点击手机号 Continue/続行 按钮：%s，等待进入短信验证码页", submit_info)
                _wait_page_settle_after_submit()

                # 等待页面进入 phone-verification；若号码无效/无法发送/WhatsApp 通道，立即换号。
                _wait_after_phone_send(driver, timeout=15)
                logger.info("[Codex][Browser] 已进入手机验证码页")

                sms_provider.set_status(activation_id, 1, http=http)
                logger.info(
                    "[Codex][Browser] 短信已发送，开始轮询验证码 activation_id=%s wait=%ss interval=%ss",
                    activation_id, sms_provider._cfg.SMS_CODE_WAIT, sms_provider._cfg.SMS_POLL_INTERVAL
                )
                sms_code = sms_provider.wait_for_sms_code(activation_id, http)
                logger.info("[Codex][Browser] 手机 OTP 收到：%s", sms_code)
                _type_otp(driver, sms_code)
                logger.info("[Codex][Browser] 已填写手机 OTP")
                human_delay("otp_input")
                if not _click_if_present(driver, ["button[type='submit']", "input[type='submit']"], timeout=10):
                    raise RuntimeError(f"verify_submit_missing: phone verification submit not found state={_phone_page_state(driver)}")
                logger.info("[Codex][Browser] 已提交手机 OTP，等待验证结果")
                otp_outcome = _wait_after_phone_otp_submit(driver, timeout=25)
                logger.info("[Codex][Browser] 手机 OTP 提交后状态：%s", otp_outcome)
                sms_provider.complete(activation_id, http)
                return
            except sms_provider.SmsNoBalanceError:
                if activation_id:
                    try:
                        sms_provider.cancel(activation_id, http)
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if isinstance(exc, sms_provider.SmsNoNumbersError) and not activation_id:
                    if selector is not None and country is not None:
                        selector.record_no_numbers(country)
                        logger.warning(
                            "[Codex][Browser] 国家 %s 暂无号码，立即切换国家（实际号码尝试仍为 %s/%s）",
                            country,
                            actual_attempt,
                            max_retries,
                        )
                        continue
                    fixed_no_numbers_attempt += 1
                    last_err = exc
                    logger.warning(
                        "[Codex][Browser] 固定 SMS 通道暂无号码，取号重试 %s/%s（实际号码尝试仍为 %s/%s）",
                        fixed_no_numbers_attempt,
                        max_retries,
                        actual_attempt,
                        max_retries,
                    )
                    if fixed_no_numbers_attempt >= max_retries:
                        raise
                    continue
                last_err = exc
                if not activation_id:
                    raise
                if selector is not None and country is not None:
                    failure_count = selector.record_number_failure(country)
                    logger.warning(
                        "[Codex][Browser] 国家 %s 实际号码失败次数=%s，routing=%s",
                        country,
                        failure_count,
                        selector.last_reason,
                    )
                logger.warning(
                    "[Codex][Browser] 手机验证尝试 %s/%s 失败，换号：%s",
                    actual_attempt,
                    max_retries,
                    str(exc)[:240],
                )
                if activation_id:
                    try:
                        sms_provider.cancel(activation_id, http)
                    except Exception:
                        pass
                if "invalid_auth_step" in str(exc):
                    raise RuntimeError(
                        "手机号流程进入 invalid_auth_step，说明授权状态还未从 email-verification 正常跳转或已失效；"
                        "已停止继续换号，避免继续消耗号码"
                    ) from exc
                # 如果已经离开手机号/验证码相关页面，认为通过或不再需要；
                # 如果仍在 phone-verification，则下一轮必须回 add-phone 重新填新号码再提交。
                try:
                    if _is_phone_code_page(driver):
                        logger.info("[Codex][Browser] 当前仍在手机验证码页，下一轮将返回 add-phone 重新设置新号码")
                    else:
                        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
                except Exception:
                    if _is_add_phone_page(driver) or _is_phone_code_page(driver):
                        logger.info("[Codex][Browser] 仍处于手机号流程，继续换号重试")
                    else:
                        logger.info("[Codex][Browser] 手机输入页已消失，继续后续流程")
                        return
                if actual_attempt < max_retries:
                    _refresh_add_phone_for_retry(driver, reason=str(exc)[:120])
                _sleep_before_phone_retry(actual_attempt, max_retries)
        raise RuntimeError(f"Roxy 手机验证实际号码重试 {actual_attempt}/{max_retries} 次仍失败，最后错误：{last_err}")
    finally:
        try:
            http.close()
        except Exception:
            pass


def _finish_consent_workspace(driver) -> str:
    """点击 Codex consent/workspace 页面里的继续/允许按钮，直到 callback。"""
    end = time.time() + int(_roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT)
    while time.time() < end:
        callback = _extract_callback_url_from_any_window(driver)
        if callback:
            return callback
        current = str(driver.current_url or "")
        clicked = False
        for selectors in [
            ["//button[contains(., 'Allow')]", "//button[contains(., 'Authorize')]", "//button[contains(., 'Continue')]"],
            ["//button[contains(., 'Select')]", "//button[contains(., 'Use workspace')]", "//button[contains(., 'Confirm')]"],
            ["//button[contains(., '允许')]", "//button[contains(., '授权')]", "//button[contains(., '继续')]", "//button[contains(., '确认')]"],
            ["button[type='submit']"],
        ]:
            if _click_if_present(driver, selectors, timeout=2):
                clicked = True
                human_delay("form")
                break
        if not clicked:
            time.sleep(0.8)
    return _wait_for_callback(driver, timeout=5)




def clear_roxy_browser_auth_state(driver) -> None:
    """清空当前 Roxy 浏览器里的 OpenAI/ChatGPT 登录态与缓存，用于注册后复用同一环境跑 Codex。"""
    origins = [
        "https://auth.openai.com",
        "https://chatgpt.com",
        "https://openai.com",
        "https://platform.openai.com",
    ]
    logger.info("[Codex][Browser] 复用注册窗口：开始清理 Cookie / localStorage / sessionStorage / cache")
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        logger.info("[Codex][Browser] 已清理浏览器 Cookie")
    except Exception as exc:
        logger.info("[Codex][Browser] 清理 Cookie 失败，继续尝试其它缓存：%s", str(exc)[:160])
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        logger.info("[Codex][Browser] 已清理浏览器 Cache")
    except Exception as exc:
        logger.info("[Codex][Browser] 清理 Cache 失败，继续：%s", str(exc)[:160])
    for origin in origins:
        try:
            driver.execute_cdp_cmd("Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": "all",
            })
            logger.info("[Codex][Browser] 已清理站点数据：%s", origin)
        except Exception as exc:
            logger.debug("[Codex][Browser] 清理站点数据失败 %s: %s", origin, exc)
    try:
        driver.get("about:blank")
    except Exception:
        pass
    time.sleep(1.0)
    logger.info("[Codex][Browser] 注册窗口登录态清理完成，准备开始 Codex 授权")

def _run_roxy_codex_oauth_once(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
) -> dict:
    """指纹浏览器 Codex OAuth 入口。

    existing_driver/existing_opened 用于“注册成功后立刻跑 Codex”：
    复用注册时的 Roxy 窗口，不新建环境，只清理浏览器状态后开始授权。
    """
    from core import codex_oauth as proto

    if not force and not proto._cfg.ENABLE_CODEX_AUTO:
        return proto._codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return proto._codex_result(status="skipped", message="email 为空")
    if otp_provider is None:
        otp_provider = wait_for_otp

    client = None if reuse_existing_profile else RoxyBrowserClient()
    opened = existing_opened if reuse_existing_profile else client.open_profile()
    browser_kind_token = _CODEX_BROWSER_KIND.set(_detect_browser_kind(opened))
    driver = existing_driver if reuse_existing_profile else None
    owns_driver = not reuse_existing_profile
    token_session = None
    try:
        auth_source = proto._codex_auth_url_source()
        code_verifier = None
        if auth_source == "cpa":
            cpa_auth = proto._request_cpa_authorize_url()
            state = cpa_auth["state"]
            auth_url = cpa_auth["auth_url"]
            logger.info("[Codex][Browser] 当前使用 CPA 授权地址: %s", auth_url)
        elif auth_source == "sub2":
            sub2_auth = proto._request_sub2_authorize_url()
            state = sub2_auth["state"]
            auth_url = sub2_auth["auth_url"]
            logger.info("[Codex][Browser] 当前使用 sub2 授权地址: %s", auth_url)
        elif auth_source == "local":
            code_verifier, code_challenge = proto._generate_pkce()
            state = proto._generate_state()
            auth_url = proto._build_authorize_url(state, code_challenge, prompt="login")
            logger.info("[Codex][Browser] 当前使用本地 PKCE 授权地址: %s", auth_url)
        else:
            raise RuntimeError(f"[Codex][Browser] 不支持的 CODEX_AUTH_URL_SOURCE={auth_source!r}")

        if auth_source == "local":
            token_proxy = _resolve_token_exchange_proxy(opened, proxy)
            token_session = proto.BrowserSession(proxy=token_proxy, detect_exit_geo=False)
            proxy_source = "Roxy 环境代理" if getattr(opened, "registration_proxy", None) else ("显式代理" if proxy else "代理池")
            logger.info("[Codex][Browser] Token 交换将复用%s，先执行免费网络预检", proxy_source)
            _preflight_token_exchange_transport(token_session, proto._cfg.CODEX_TOKEN_URL)

        if not driver:
            driver = _build_driver(opened)
            _center_browser_window(driver)
        driver.set_page_load_timeout(int(_roxy_cfg.ROXY_SELENIUM_TIMEOUT))
        logger.info("[Codex][Browser] 开始授权：%s，profile=%s，reuse_existing_profile=%s", email, opened.profile_id, reuse_existing_profile)
        if reuse_existing_profile and clear_existing_state:
            clear_roxy_browser_auth_state(driver)

        _fill_email_and_otp(driver, email, otp_provider, auth_url)
        human_delay("api")
        logger.info("[Codex][Browser] 检查是否需要手机号验证")
        _do_phone_verification_if_present(driver)
        logger.info("[Codex][Browser] 手机验证处理完成/无需处理，等待授权确认和 callback")
        callback_url = _finish_consent_workspace(driver)
        code = proto._extract_code(callback_url, state)
        logger.info("[Codex][Browser] 已捕获 callback code：%s...", code[:24])

        if auth_source == "cpa":
            submit_payload = proto._submit_cpa_callback(callback_url)
            path = proto._save_cpa_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url,
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "CPA callback submitted"
            return proto._codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=f"{_codex_driver_name()}: {msg}",
            )

        if auth_source == "sub2":
            submit_payload = proto._submit_sub2_callback(
                callback_url,
                session_id=(sub2_auth or {}).get("session_id", ""),
                redirect_uri=(proto.parse_qs(proto.urlparse(auth_url or "").query).get("redirect_uri") or [""])[0],
            )
            path = proto._save_sub2_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url,
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "sub2 callback uploaded"
            return proto._codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=f"{_codex_driver_name()}: {msg}",
            )

        if not code_verifier:
            raise RuntimeError("[Codex][Browser] local 模式缺少 code_verifier")
        session = token_session or proto.BrowserSession(
            proxy=_resolve_token_exchange_proxy(opened, proxy),
            detect_exit_geo=False,
        )
        token_resp = proto.exchange_codex_token(session, code, code_verifier)
        id_claims = proto._parse_id_token(token_resp.get("id_token", ""))
        effective_email = id_claims.get("email") or email
        storage = proto.build_codex_storage(token_resp, id_claims)
        path = proto.save_codex_credential(storage, effective_email, id_claims.get("plan_type", ""))
        return proto._codex_result(
            status="success",
            ok=True,
            email=effective_email,
            file_path=str(path),
            callback_url=callback_url,
            message=f"{_codex_driver_name()} plan={id_claims.get('plan_type') or 'unknown'}",
        )
    except AccountUnusableError as exc:
        logger.warning("[Codex][Browser] 账号已废：%s，%s", email, exc.error_code)
        return proto._codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code or 'account_deactivated'}）",
        )
    except Exception as exc:
        logger.warning("[Codex][Browser] 失败：%s，%s: %s", email, type(exc).__name__, str(exc)[:240])
        logger.debug("[Codex][Browser] 失败详情", exc_info=True)
        return proto._codex_result(status="failed", email=email, message=f"{type(exc).__name__}: {str(exc)[:220]}")
    finally:
        # 注册后复用窗口时，driver/profile 生命周期由注册流程统一清理，
        # 这里不能 quit/delete，否则会提前销毁注册环境。
        if owns_driver and driver and not bool(_roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if owns_driver and client and not bool(_roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
        try:
            _CODEX_BROWSER_KIND.reset(browser_kind_token)
        except Exception:
            pass


def run_roxy_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
) -> dict:
    """指纹浏览器 Codex OAuth 入口；CPA callback 409 timeout 时重新开启一轮授权。"""
    from core import codex_oauth as proto

    max_rounds = 2
    last_result = None
    for round_no in range(1, max_rounds + 1):
        if round_no > 1:
            logger.warning(
                "[Codex][Browser] CPA callback 返回 Timeout waiting for OAuth callback，重新开启第 %s/%s 轮 Codex 授权：%s",
                round_no, max_rounds, email,
            )
        result = _run_roxy_codex_oauth_once(
            email=email,
            otp_provider=otp_provider,
            proxy=proxy,
            force=force,
            existing_driver=existing_driver,
            existing_opened=existing_opened,
            reuse_existing_profile=reuse_existing_profile,
            clear_existing_state=clear_existing_state,
        )
        last_result = result
        if result.get("ok"):
            return result
        msg = result.get("message") or result.get("error") or ""
        if not proto._is_cpa_callback_reauth_error(msg):
            return result
    if last_result:
        last_result = dict(last_result)
        last_result["message"] = f"CPA callback 超时，已重新授权 {max_rounds} 轮仍失败：{last_result.get('message') or ''}"
        return last_result
    return proto._codex_result(status="failed", email=email, message="CPA callback 超时，重新授权失败")
