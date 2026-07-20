# -*- coding: utf-8 -*-
"""通过 Browser Use Cloud + Playwright 执行 Codex OAuth 授权。"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

from config import browser_use as _cfg
from config import roxybrowser as _roxy_cfg
from core import sms_provider
from core.browser_use_client import BrowserUseClient
from core.openai_auth import AccountUnusableError, detect_account_unusable_response_body
from core.browser_use_registration import (
    _timeout_ms,
    _page_url,
    _fill_first,
    _click_first,
    _maybe_accept_cookies,
    _type_otp,
    _clear_otp_inputs,
    _wait_after_otp,
)
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)

_LOG_CONTEXT = threading.local()


def _log_provider_label() -> str:
    return str(getattr(_LOG_CONTEXT, "provider_label", "BrowserUse") or "BrowserUse")


def _set_log_provider_label(label: str) -> None:
    _LOG_CONTEXT.provider_label = label or "BrowserUse"


class _CloudProviderLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        label = _log_provider_label()
        if label != "BrowserUse" and isinstance(record.msg, str):
            record.msg = record.msg.replace("[BrowserUse]", f"[{label}]").replace("BrowserUse", label)
        return True


logger.addFilter(_CloudProviderLogFilter())


def _fast_mode() -> bool:
    return bool(getattr(_cfg, "BROWSER_USE_FAST_MODE", True))


def _log_timing_enabled() -> bool:
    return bool(getattr(_cfg, "BROWSER_USE_LOG_TIMING", True))


def _bu_delay(kind: str, seconds: float | None = None) -> None:
    """Browser Use 专用轻量延迟。fast mode 下只保留极短 DOM 稳定等待。"""
    if _fast_mode():
        if seconds is None:
            seconds = {
                "navigate": 0.2,
                "form": 0.12,
                "otp_input": 0.15,
                "api": 0.15,
                "post_auth": 0.2,
            }.get(kind, 0.1)
        if seconds > 0:
            time.sleep(seconds)
        return
    human_delay(kind)


class _StepTimer:
    def __init__(self, label: str):
        self.label = label
        self.t0 = time.perf_counter()
        if _log_timing_enabled():
            logger.info("[Codex][BrowserUse][耗时] %s 开始", label)

    def done(self, extra: str = "") -> None:
        if _log_timing_enabled():
            cost = time.perf_counter() - self.t0
            logger.info("[Codex][BrowserUse][耗时] %s 完成 %.2fs%s", self.label, cost, (" " + extra) if extra else "")



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


def _extract_callback_url_from_page(page) -> str:
    try:
        current = str(page.url or "")
        if _is_callback_url(current):
            return current
    except Exception:
        pass
    try:
        urls = page.evaluate(
            """() => {
              const out = [];
              const push = v => { if (v && typeof v === 'string') out.push(v); };
              try { push(location.href); } catch (e) {}
              try { push(document.URL); } catch (e) {}
              try { push(document.documentURI); } catch (e) {}
              try { for (const e of performance.getEntriesByType('navigation')) push(e.name); } catch (e) {}
              try { for (const e of performance.getEntries()) push(e.name); } catch (e) {}
              return [...new Set(out)];
            }"""
        ) or []
        for url in urls:
            if _is_callback_url(str(url)):
                logger.info("[Codex][BrowserUse] 已从页面性能记录提取 callback URL：%s", str(url)[:160])
                return str(url)
    except Exception as exc:
        logger.debug("[Codex][BrowserUse] 提取 callback URL 失败：%s", exc)
    return ""


def _extract_callback_url_from_context(context, page=None) -> str:
    pages = []
    if page is not None:
        pages.append(page)
    try:
        pages.extend([p for p in context.pages if p not in pages])
    except Exception:
        pass
    for p in pages:
        try:
            found = _extract_callback_url_from_page(p)
            if found:
                return found
        except Exception:
            continue
    return ""


def _wait_for_callback(context, page, timeout: int | None = None) -> str:
    end = time.time() + (timeout or int(getattr(_roxy_cfg, "ROXY_CODEX_CALLBACK_TIMEOUT", 180) or 180))
    last_url = ""
    while time.time() < end:
        try:
            current = str(page.url or "")
            if current != last_url:
                logger.debug("[Codex][BrowserUse] 当前 URL: %s", current)
                last_url = current
            callback = _extract_callback_url_from_context(context, page)
            if callback:
                return callback
        except Exception:
            pass
        time.sleep(0.25 if _fast_mode() else 0.5)
    raise RuntimeError(f"等待 Codex callback 超时，最后 URL={last_url}")


def _wait_for_fresh_email_otp(otp_provider, email: str, after_ts: float, used_codes: set[str] | None = None, timeout: int = 90) -> str:
    """获取一个未提交过的邮箱 OTP，避免重发后复用旧码。"""
    used_codes = {str(x) for x in (used_codes or set()) if x}
    end = time.time() + timeout
    last_code = ""
    while time.time() < end:
        code = str(otp_provider(email, after_ts=after_ts) or "").strip()
        if code:
            last_code = code
            if code not in used_codes:
                return code
        time.sleep(1 if _fast_mode() else 2)
    if last_code:
        raise RuntimeError(f"等待邮箱 OTP 超时，最后只拿到已使用验证码：{last_code}")
    raise RuntimeError("等待邮箱 OTP 超时")




def _all_frames(page):
    frames = [page]
    try:
        frames.extend([f for f in page.frames if f not in frames])
    except Exception:
        pass
    return frames


def _wait_auth_page_ready(page, timeout: int = 8) -> None:
    """等待 auth.openai.com 登录页真正渲染；Browser Use/CDP 有时 domcontentloaded 后 body 仍为空。"""
    end = time.time() + timeout
    last_url = ""
    while time.time() < end:
        try:
            last_url = _page_url(page)
            # 任意 frame 出现 input/button/body 文本即认为可操作
            for frame in _all_frames(page):
                try:
                    if frame.locator("input, button, textarea, [role='button']").count() > 0:
                        return
                except Exception:
                    pass
                try:
                    text = (frame.locator("body").inner_text(timeout=500) or "").strip()
                    if text:
                        return
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(0.25 if _fast_mode() else 0.6)
    logger.warning("[Codex][BrowserUse] 登录页等待渲染超时，最后 URL=%s", last_url or "-")


def _visible_locator_any_frame(page, selectors: list[str], timeout_ms: int = 1000):
    for frame in _all_frames(page):
        for selector in selectors:
            try:
                loc = frame.locator(selector).first
                if loc.count() == 0:
                    continue
                if loc.is_visible(timeout=timeout_ms):
                    return loc
            except Exception:
                continue
    return None


def _click_first_any_frame(page, selectors: list[str], timeout_ms: int = 5000) -> bool:
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        loc = _visible_locator_any_frame(page, selectors, timeout_ms=700)
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
        time.sleep(0.25)
    return False


def _fill_first_any_frame(page, selectors: list[str], value: str, timeout_ms: int = 10000) -> bool:
    end = time.time() + timeout_ms / 1000
    last_err = None
    while time.time() < end:
        loc = _visible_locator_any_frame(page, selectors, timeout_ms=700)
        if loc is not None:
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=2000)
                loc.fill(value, timeout=5000)
                return True
            except Exception as exc:
                last_err = exc
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
        time.sleep(0.25)
    if last_err:
        logger.debug("[Codex][BrowserUse] fill any-frame failed: %s", last_err)
    return False


def _js_fill_email_fallback(page, email: str) -> bool:
    """最后兜底：在所有 frame 里扫描可见 input，填第一个疑似邮箱/用户名输入框。"""
    script = r"""
    (email) => {
      const isVisible = (el) => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 5 && r.height > 5;
      };
      const inputs = [...document.querySelectorAll('input')].filter(isVisible);
      const score = (el) => {
        const hay = [el.type, el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute('aria-label')].join(' ').toLowerCase();
        if (/(email|mail|username|loginfmt|identifier|メール|邮箱|電子郵件)/i.test(hay)) return 100;
        if (!el.type || ['text','email','search'].includes((el.type||'').toLowerCase())) return 20;
        return 0;
      };
      const target = inputs.map(el => [score(el), el]).filter(x => x[0] > 0).sort((a,b) => b[0]-a[0])[0]?.[1];
      if (!target) return false;
      const proto = window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      target.focus();
      if (setter) setter.call(target, email); else target.value = email;
      target.dispatchEvent(new Event('input', {bubbles:true}));
      target.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    }
    """
    for frame in _all_frames(page):
        try:
            if frame.evaluate(script, email):
                return True
        except Exception:
            continue
    return False

def _body_snippet(page, limit: int = 600) -> str:
    chunks = []
    for frame in _all_frames(page):
        try:
            text = frame.locator("body").inner_text(timeout=1200) or ""
            text = " ".join(text.split())
            if text:
                chunks.append(text)
        except Exception:
            pass
    return " | ".join(chunks)[:limit]


def _current_state_for_log(page) -> str:
    return f"url={_page_url(page) or '-'} body={_body_snippet(page, 500) or '-'}"


def _looks_next_step_after_login(page) -> bool:
    url = _page_url(page).lower()
    if _is_callback_url(url):
        return True
    if any(x in url for x in ("phone", "add-phone", "phone-verification", "workspace", "consent", "localhost:1455")):
        return True
    try:
        body = (page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        body = ""
    return any(
        x in body
        for x in (
            "phone number", "verify your phone", "workspace", "authorize", "allow", "consent",
            "手机号", "电话号码", "ワークスペース", "認証", "許可",
        )
    )



def _click_email_entry_fast(page) -> bool:
    script = r"""
    () => {
      const isVisible = (el) => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 5 && r.height > 5;
      };
      const nodes = [...document.querySelectorAll('button,a,[role=button]')].filter(isVisible);
      const score = (el) => {
        const hay = [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('data-provider'), el.getAttribute('data-testid')].join(' ').toLowerCase();
        if (/(continue|sign|log).{0,20}(email|mail)|email|mail|メール|邮箱|電子郵件/i.test(hay)) return 100;
        return 0;
      };
      const target = nodes.map(el => [score(el), el]).filter(x => x[0] > 0).sort((a,b)=>b[0]-a[0])[0]?.[1];
      if (!target) return false;
      target.scrollIntoView({block:'center'});
      target.click();
      return true;
    }
    """
    for frame in _all_frames(page):
        try:
            if frame.evaluate(script):
                return True
        except Exception:
            continue
    return False


def _fill_email_fast(page, email: str) -> bool:
    script = r"""
    (email) => {
      const isVisible = (el) => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 5 && r.height > 5;
      };
      const inputs = [...document.querySelectorAll('input')].filter(isVisible);
      const score = (el) => {
        const hay = [el.type, el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute('aria-label')].join(' ').toLowerCase();
        if (/(email|mail|username|loginfmt|identifier|メール|邮箱|電子郵件)/i.test(hay)) return 100;
        if (!el.type || ['text','email','search'].includes((el.type||'').toLowerCase())) return 20;
        return 0;
      };
      const target = inputs.map(el => [score(el), el]).filter(x => x[0] > 0).sort((a,b)=>b[0]-a[0])[0]?.[1];
      if (!target) return false;
      const proto = window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      target.focus();
      if (setter) setter.call(target, email); else target.value = email;
      target.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data: email}));
      target.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    }
    """
    for frame in _all_frames(page):
        try:
            if frame.evaluate(script, email):
                return True
        except Exception:
            continue
    return False

def _click_email_entry_if_present(page) -> None:
    # OAuth 登录页在不同地区会先显示“Continue with email/メールで続行”等入口。
    if _click_email_entry_fast(page):
        time.sleep(0.3)
        return
    _click_first_any_frame(
        page,
        [
            "button[data-testid*='email' i]",
            "button[data-provider='email']",
            "a[data-provider='email']",
            "button:has-text('Continue with email')",
            "button:has-text('Sign up with email')",
            "button:has-text('Log in with email')",
            "button:has-text('Email')",
            "a:has-text('Continue with email')",
            "a:has-text('Sign up with email')",
            "button:has-text('メールで続行')",
            "button:has-text('メールアドレスで続行')",
            "button:has-text('メール')",
            "a:has-text('メールで続行')",
            "button:has-text('使用邮箱')",
            "button:has-text('使用電子郵件')",
            "button:has-text('邮箱')",
            "button:has-text('電子郵件')",
        ],
        timeout_ms=1500,
    )



def _submit_visible_form_or_enter(page) -> bool:
    script = r"""
    () => {
      const isVisible = (el) => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 5 && r.height > 5;
      };
      const buttons = [...document.querySelectorAll('button,input[type=submit],[role=button]')].filter(isVisible);
      const score = (el) => {
        const text = [el.innerText, el.value, el.getAttribute('aria-label')].join(' ').toLowerCase();
        if (/(continue|next|submit|sign in|log in|続行|次へ|送信|继续|下一步|登录|登入)/i.test(text)) return 100;
        if ((el.type || '').toLowerCase() === 'submit') return 90;
        return 0;
      };
      const target = buttons.map(el => [score(el), el]).filter(x => x[0] > 0).sort((a,b)=>b[0]-a[0])[0]?.[1];
      if (target) { target.click(); return true; }
      const form = document.querySelector('form');
      if (form) { form.requestSubmit ? form.requestSubmit() : form.submit(); return true; }
      return false;
    }
    """
    for frame in _all_frames(page):
        try:
            if frame.evaluate(script):
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False

def _fill_email_for_codex(page, email: str) -> None:
    # 页面已渲染时优先快速处理，避免 Browser Use/CDP 的长 timeout 叠加导致卡几十秒。
    # 先不等 selector，直接 JS 扫描可见 input；页面已渲染时最快。
    if not _fill_email_fast(page, email):
        _click_email_entry_if_present(page)
        # 点击邮箱入口后立即再尝试一次。
        _fill_email_fast(page, email)
    selectors = [
        "input[type='email']",
        "input[name='email']",
        "input[name='username']",
        "input[name='loginfmt']",
        "input[name='identifier']",
        "input[id='username']",
        "input[id*='email' i]",
        "input[id*='username' i]",
        "input[autocomplete='email']",
        "input[autocomplete='username']",
        "input[inputmode='email']",
        "input[aria-label*='email' i]",
        "input[aria-label*='メール']",
        "input[aria-label*='邮箱']",
        "input[placeholder*='email' i]",
        "input[placeholder*='メール']",
        "input[placeholder*='邮箱']",
        "input[placeholder*='電子郵件']",
    ]
    ok = _fill_email_fast(page, email)
    if not ok:
        ok = _fill_first_any_frame(page, selectors, email, timeout_ms=1200)
    if not ok:
        ok = _js_fill_email_fallback(page, email)
    if not ok:
        # 最后再给 React/hydration 一小段时间，不再等很久。
        end = time.time() + 4
        while time.time() < end and not ok:
            ok = _fill_email_fast(page, email) or _fill_first_any_frame(page, selectors, email, timeout_ms=800)
            if ok:
                break
            time.sleep(0.25)
    if not ok:
        raise RuntimeError("找不到邮箱输入框；" + _current_state_for_log(page))

    clicked = _click_first_any_frame(
        page,
        [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('Submit')",
            "button:has-text('続行')",
            "button:has-text('次へ')",
            "button:has-text('送信')",
            "button:has-text('继续')",
            "button:has-text('下一步')",
            "form button",
        ],
        timeout_ms=1200,
    )
    if not clicked:
        _submit_visible_form_or_enter(page)
    _bu_delay("form")


def _looks_email_otp_page(page) -> bool:
    url = _page_url(page).lower()
    if "email-verification" in url or "email_otp" in url or ("verify" in url and "email" in url):
        return True
    try:
        return page.locator("input[autocomplete='one-time-code'], input[name='code'], input[inputmode='numeric']").count() > 0
    except Exception:
        return False


def _install_account_dead_response_tracker(page) -> dict:
    """监听浏览器里的 email-otp/validate 响应，用于识别账号已废。"""
    tracker = {"code": "", "text": ""}
    try:
        def _on_response(resp):
            try:
                if "email-otp/validate" not in str(getattr(resp, "url", "")):
                    return
                text = ""
                if int(getattr(resp, "status", 0) or 0) != 200:
                    try:
                        text = resp.text() or ""
                    except Exception:
                        text = ""
                code = detect_account_unusable_response_body(text)
                if code:
                    tracker["code"] = code
                    tracker["text"] = text[:500]
                    logger.warning("[Codex][BrowserUse] email-otp/validate 响应识别账号已废：%s", code)
            except Exception:
                pass
        page.on("response", _on_response)
    except Exception:
        pass
    return tracker


def _wait_after_email_submit(page, timeout: int = 45, dead_tracker: dict | None = None) -> str:
    end = time.time() + timeout
    while time.time() < end:
        if dead_tracker and dead_tracker.get("code"):
            return f"deactivated:{dead_tracker.get('code')}"
        url = _page_url(page).lower()
        if _is_callback_url(url):
            return "callback"
        if any(x in url for x in ("phone", "workspace", "consent", "authorize", "localhost:1455")):
            return "accepted"
        if not _looks_email_otp_page(page) and ("auth.openai.com" in url or "chatgpt.com" in url):
            return "accepted"
        body = ""
        try:
            body = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            pass
        if any(x in body for x in ("incorrect", "invalid", "expired", "错误", "过期", "无效")):
            return "invalid"
        time.sleep(0.5)
    return "unknown"


def _fill_email_and_otp(page, email: str, otp_provider, auth_url: str, dead_tracker: dict | None = None) -> None:
    otp_after_ts = time.time()
    logger.info("[Codex][BrowserUse] 打开授权地址")
    logger.info("[Codex][BrowserUse] 完整授权地址: %s", auth_url)
    _t_goto = _StepTimer("打开授权页")
    page.goto(auth_url, wait_until="domcontentloaded", timeout=_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
    try:
        page.wait_for_load_state("load", timeout=5000)
    except Exception:
        pass
    _wait_auth_page_ready(page, timeout=2)
    _t_goto.done(f"url={_page_url(page) or '-'}")
    _bu_delay("navigate")
    _maybe_accept_cookies(page)

    try:
        _t_email = _StepTimer("填写并提交邮箱")
        _fill_email_for_codex(page, email)
        _t_email.done()
        logger.info("[Codex][BrowserUse] 已提交邮箱：%s", email)
    except Exception as exc:
        if _looks_next_step_after_login(page):
            logger.info("[Codex][BrowserUse] 未检测到邮箱输入框，但页面已进入后续授权步骤：%s", _current_state_for_log(page))
            return
        logger.error("[Codex][BrowserUse] 未检测到邮箱输入框，当前页面状态：%s", _current_state_for_log(page))
        raise RuntimeError(f"Codex BrowserUse 授权页未出现邮箱输入框：{str(exc)[:220]}") from exc

    used_codes: set[str] = set()

    def _restart_email_otp_flow(reason: str) -> None:
        """Codex Auth 上直接点 resend 可能触发服务端 500；改为重开授权地址并重新提交邮箱。"""
        nonlocal otp_after_ts
        logger.info("[Codex][BrowserUse] 重新触发邮箱 OTP：%s", reason)
        otp_after_ts = time.time()
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
            _bu_delay("navigate")
            _maybe_accept_cookies(page)
            if _looks_email_otp_page(page) or _looks_next_step_after_login(page):
                logger.info("[Codex][BrowserUse] 重开授权后已在 OTP/下一步页面：%s", _current_state_for_log(page))
                return
            _fill_email_for_codex(page, email)
            logger.info("[Codex][BrowserUse] 已重新提交邮箱触发 OTP")
        except Exception as exc:
            logger.warning("[Codex][BrowserUse] 重新提交邮箱失败，继续按当前页面轮询：%s", str(exc)[:180])
        _bu_delay("api")

    for attempt in range(1, 4):
        wait_end = time.time() + 35
        while time.time() < wait_end and not _looks_email_otp_page(page):
            if any(x in _page_url(page).lower() for x in ("phone", "workspace", "consent", "localhost:1455")):
                return
            time.sleep(0.4)
        logger.info("[Codex][BrowserUse] 等待邮箱 OTP：%s（%s/3）", email, attempt)
        _t_otp_wait = _StepTimer("等待邮箱 OTP")
        try:
            code = _wait_for_fresh_email_otp(otp_provider, email, after_ts=otp_after_ts, used_codes=used_codes, timeout=90)
            _t_otp_wait.done()
        except Exception as exc:
            _t_otp_wait.done(f"failed={type(exc).__name__}: {str(exc)[:160]}")
            if attempt >= 3:
                raise
            logger.warning(
                "[Codex][BrowserUse] 一直未收到邮箱 OTP，点击“重新发送电子邮件”后继续等待（下一轮 %s/3）：%s: %s",
                attempt + 1,
                type(exc).__name__,
                str(exc)[:180],
            )
            _restart_email_otp_flow("等待验证码超时，避免点击 resend 导致 500")
            continue
        used_codes.add(str(code))
        logger.info("[Codex][BrowserUse] 邮箱 OTP 收到：%s", code)
        _t_otp_submit = _StepTimer("提交邮箱 OTP")
        _clear_otp_inputs(page)
        _type_otp(page, code)
        _bu_delay("otp_input")
        _click_first_any_frame(
            page,
            [
                "button[type='submit']",
                "button:has-text('Continue')",
                "button:has-text('Verify')",
                "button:has-text('Submit')",
                "button:has-text('続行')",
                "button:has-text('送信')",
                "button:has-text('继续')",
                "button:has-text('验证')",
                "form button",
            ],
            timeout_ms=4000,
        )
        outcome = _wait_after_email_submit(page, timeout=30 if _fast_mode() else 45, dead_tracker=dead_tracker)
        _t_otp_submit.done(f"state={outcome}")
        logger.info("[Codex][BrowserUse] 邮箱 OTP 提交后状态：%s", outcome)
        if str(outcome).startswith("deactivated:"):
            error_code = str(outcome).split(":", 1)[1] or "account_deactivated"
            raise AccountUnusableError(f"账号已废（{error_code}）", error_code=error_code)
        if outcome in ("accepted", "callback", "unknown"):
            return
        if attempt >= 3:
            raise RuntimeError("Codex 邮箱验证码连续错误/过期")
        _restart_email_otp_flow("验证码错误/过期或页面未跳转，避免点击 resend 导致 500")


def _select_sms_channel(page) -> None:
    try:
        page.evaluate(
            """() => {
              const radios = [...document.querySelectorAll('input[type=radio]')];
              const sms = radios.find(el => /^(sms|text|text_message|text-message)$/i.test(el.value || ''));
              if (sms) { sms.click(); sms.dispatchEvent(new Event('input', {bubbles:true})); sms.dispatchEvent(new Event('change', {bubbles:true})); }
            }"""
        )
    except Exception:
        pass


def _has_phone_prompt(page) -> bool:
    url = _page_url(page).lower()
    if any(x in url for x in ("phone", "add-phone", "phone-verification")):
        return True
    try:
        body = (page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        body = ""
    return any(x in body for x in ("phone number", "verify your phone", "手机号", "电话号码"))



def _phone_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _phone_e164(value: str) -> str:
    digits = _phone_digits(value)
    if not digits:
        return ""
    return "+" + digits


def _read_phone_input_value(page) -> str:
    selectors = [
        "input[type='tel']",
        "input[name*='phone' i]",
        "input[autocomplete='tel']",
        "input[aria-label*='phone' i]",
        "input[placeholder*='phone' i]",
        "input[placeholder*='手机号']",
    ]
    loc = _visible_locator_any_frame(page, selectors, timeout_ms=700)
    if loc is None:
        return ""
    try:
        return str(loc.input_value(timeout=1000) or "")
    except Exception:
        try:
            return str(loc.evaluate("el => el.value || ''") or "")
        except Exception:
            return ""


def _force_set_phone_value(page, phone_e164: str) -> bool:
    script = r"""
    (phone) => {
      const isVisible = (el) => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 5 && r.height > 5;
      };
      const inputs = [...document.querySelectorAll('input')].filter(isVisible);
      const score = (el) => {
        const hay = [el.type, el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute('aria-label')].join(' ').toLowerCase();
        if (/(phone|tel|mobile|sms|手机号|手机|電話|携帯)/i.test(hay)) return 100;
        if ((el.type || '').toLowerCase() === 'tel') return 90;
        return 0;
      };
      const target = inputs.map(el => [score(el), el]).filter(x => x[0] > 0).sort((a,b) => b[0]-a[0])[0]?.[1];
      if (!target) return false;
      const proto = window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      target.focus();
      if (setter) setter.call(target, phone); else target.value = phone;
      target.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data: phone}));
      target.dispatchEvent(new Event('change', {bubbles:true}));
      target.dispatchEvent(new Event('blur', {bubbles:true}));
      return true;
    }
    """
    for frame in _all_frames(page):
        try:
            if frame.evaluate(script, phone_e164):
                return True
        except Exception:
            continue
    return False


def _phone_error_text(page) -> str:
    body = _body_snippet(page, 1200).lower()
    bad_words = [
        "invalid", "unsupported", "unable", "can't", "cannot", "error", "try again", "not valid",
        "too many", "already", "blocked", "拒", "无效", "错误", "不支持", "できません", "無効",
    ]
    if any(w in body for w in bad_words):
        return body[:500]
    return ""


def _has_visible_phone_code_input(page) -> bool:
    loc = _visible_locator_any_frame(
        page,
        [
            "input[autocomplete='one-time-code']",
            "input[name='code']",
            "input[name='otp']",
            "input[inputmode='numeric']",
            "input[aria-label*='code' i]",
            "input[placeholder*='code' i]",
            "input[aria-label*='验证码']",
            "input[placeholder*='验证码']",
        ],
        timeout_ms=700,
    )
    if loc is None:
        return False
    body = _body_snippet(page, 800).lower()
    # 避免误把邮箱 OTP 页残留输入框当成短信页。
    if any(x in body for x in ("phone", "sms", "text message", "手机", "短信", "電話", "携帯")):
        return True
    url = _page_url(page).lower()
    return any(x in url for x in ("phone", "sms"))


def _wait_after_phone_send(page, timeout: int = 18) -> str:
    """提交手机号后的状态：code_page / rejected / still_form / callback / unknown。"""
    end = time.time() + timeout
    last_state = ""
    while time.time() < end:
        url = _page_url(page).lower()
        if _is_callback_url(url):
            return "callback"
        err = _phone_error_text(page)
        if err:
            logger.warning("[Codex][BrowserUse] 手机号提交后页面错误提示：%s", err[:240])
            return "rejected"
        if _has_visible_phone_code_input(page):
            return "code_page"
        phone_value = _read_phone_input_value(page)
        state = f"url={url} phone_value={phone_value!r} body={_body_snippet(page, 220)!r}"
        last_state = state
        time.sleep(0.7)
    logger.warning("[Codex][BrowserUse] 提交手机号后未确认进入短信页，最后状态：%s", last_state)
    # 仍然停留在可见手机号输入框，基本就是没发出去/按钮没点中/页面拒绝但未识别。
    if _read_phone_input_value(page):
        return "still_form"
    return "unknown"


def _wait_phone_form_ready(page, timeout: int = 12) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _visible_locator_any_frame(
            page,
            [
                "input[type='tel']",
                "input[name*='phone' i]",
                "input[autocomplete='tel']",
                "input[aria-label*='phone' i]",
                "input[placeholder*='phone' i]",
                "input[placeholder*='電話']",
                "input[placeholder*='手机号']",
            ],
            timeout_ms=500,
        ) is not None:
            return True
        time.sleep(0.4)
    return False


def _dismiss_phone_country_dropdown(page) -> None:
    # OpenAI 的国家码 combobox 有时会保持展开，挡住/吃掉 Continue 点击。
    try:
        page.keyboard.press("Escape")
        time.sleep(0.15)
        page.keyboard.press("Tab")
        time.sleep(0.15)
    except Exception:
        pass
    try:
        page.evaluate("""() => {
          const active = document.activeElement;
          if (active && active.blur) active.blur();
          document.body.click();
        }""")
    except Exception:
        pass


def _click_phone_continue(page) -> bool:
    _dismiss_phone_country_dropdown(page)
    # 先用 JS 点可见的主按钮，避免 Playwright locator 被 country listbox/portal 干扰。
    script = r"""
    () => {
      const isVisible = (el) => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 5 && r.height > 5;
      };
      const buttons = [...document.querySelectorAll('button,input[type=submit],[role=button]')].filter(isVisible);
      const score = (el) => {
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') return -1;
        const text = [el.innerText, el.textContent, el.value, el.getAttribute('aria-label')].join(' ').trim().toLowerCase();
        if (/(continue|send|next|verify|submit|続行|送信|次へ|確認|继续|发送|下一步|验证)/i.test(text)) return 100;
        if ((el.type || '').toLowerCase() === 'submit') return 90;
        return 0;
      };
      const target = buttons.map(el => [score(el), el]).filter(x => x[0] > 0).sort((a,b)=>b[0]-a[0])[0]?.[1];
      if (target) { target.scrollIntoView({block:'center'}); target.click(); return true; }
      const form = document.querySelector('form');
      if (form) { form.requestSubmit ? form.requestSubmit() : form.submit(); return true; }
      return false;
    }
    """
    for frame in _all_frames(page):
        try:
            if frame.evaluate(script):
                return True
        except Exception:
            continue
    return _click_first_any_frame(
        page,
        [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Continue')",
            "button:has-text('Send')",
            "button:has-text('Next')",
            "button:has-text('Verify')",
            "button:has-text('続行')",
            "button:has-text('送信')",
            "button:has-text('次へ')",
            "button:has-text('確認')",
            "button:has-text('继续')",
            "button:has-text('发送')",
            "button:has-text('下一步')",
            "form button",
        ],
        timeout_ms=2500,
    )


def _clear_phone_inputs(page) -> None:
    script = r"""
    () => {
      const inputs = [...document.querySelectorAll('input')];
      for (const el of inputs) {
        const hay = [el.type, el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute('aria-label')].join(' ').toLowerCase();
        if (/(phone|tel|mobile|sms|手机号|手机|電話|携帯)/i.test(hay) || (el.type || '').toLowerCase() === 'tel') {
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
      }
    }
    """
    for frame in _all_frames(page):
        try:
            frame.evaluate(script)
        except Exception:
            pass

def _fill_phone(page, phone: str) -> str:
    phone_e164 = _phone_e164(phone)
    if not phone_e164:
        raise RuntimeError(f"手机号为空/格式无效：{phone!r}")
    logger.info("[Codex][BrowserUse] 准备填写手机号 E.164：%s", phone_e164)
    if not _wait_phone_form_ready(page, timeout=8):
        raise RuntimeError("找不到手机号输入框；" + _current_state_for_log(page))
    selectors = [
        "input[type='tel']",
        "input[name*='phone' i]",
        "input[autocomplete='tel']",
        "input[aria-label*='phone' i]",
        "input[placeholder*='phone' i]",
        "input[placeholder*='手机号']",
        "input[aria-label*='電話']",
        "input[placeholder*='電話']",
    ]
    _clear_phone_inputs(page)
    ok = _fill_first_any_frame(page, selectors, phone_e164, timeout_ms=3500)
    if not ok:
        ok = _force_set_phone_value(page, phone_e164)
    if not ok:
        raise RuntimeError("找不到手机号输入框；" + _current_state_for_log(page))

    actual = _read_phone_input_value(page)
    if _phone_digits(actual) != _phone_digits(phone_e164):
        logger.warning(
            "[Codex][BrowserUse] 手机号输入校验不一致，尝试强制重填：expected=%s actual=%r",
            phone_e164,
            actual,
        )
        _force_set_phone_value(page, phone_e164)
        actual = _read_phone_input_value(page)
    if _phone_digits(actual) != _phone_digits(phone_e164):
        raise RuntimeError(f"手机号未正确写入页面：expected={phone_e164}, actual={actual!r}")
    logger.info("[Codex][BrowserUse] 页面手机号输入值：%r", actual)

    _select_sms_channel(page)
    if not _click_phone_continue(page):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    return phone_e164


def _wait_phone_code_page(page, timeout: int = 25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _is_callback_url(_page_url(page)):
            return False
        if _has_visible_phone_code_input(page):
            return True
        time.sleep(0.5)
    return False


def _wait_after_phone_otp(page, timeout: int = 25) -> str:
    end = time.time() + timeout
    while time.time() < end:
        url = _page_url(page).lower()
        if _is_callback_url(url):
            return "callback"
        if not any(x in url for x in ("phone", "otp", "verification")):
            return "accepted"
        try:
            body = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            body = ""
        if any(x in body for x in ("incorrect", "invalid", "expired", "错误", "过期", "无效")):
            return "invalid"
        time.sleep(0.5)
    return "unknown"



def _is_add_phone_url(page) -> bool:
    return "add-phone" in _page_url(page).lower()


def _try_click_change_phone(page) -> bool:
    return _click_first_any_frame(
        page,
        [
            "button:has-text('Change')",
            "button:has-text('Edit')",
            "button:has-text('Back')",
            "a:has-text('Change')",
            "a:has-text('Edit')",
            "a:has-text('Back')",
            "button:has-text('電話番号を変更')",
            "button:has-text('変更')",
            "button:has-text('戻る')",
            "a:has-text('電話番号を変更')",
            "a:has-text('変更')",
            "a:has-text('戻る')",
            "button:has-text('更改')",
            "button:has-text('返回')",
            "a:has-text('更改')",
            "a:has-text('返回')",
        ],
        timeout_ms=2500,
    )


def _ensure_add_phone_form(page, *, reason: str = "") -> bool:
    """确保当前回到 add-phone 手机号输入页；换号前调用，避免先取号后才发现页面空白。"""
    if _wait_phone_form_ready(page, timeout=2):
        return True

    logger.info("[Codex][BrowserUse] 准备回到手机号输入页：reason=%s url=%s", reason or "retry", _page_url(page) or "-")

    # 如果在短信验证码页，优先点击 change/back 或浏览器后退，保留 auth transaction state。
    try:
        if _try_click_change_phone(page):
            if _wait_phone_form_ready(page, timeout=8):
                return True
    except Exception:
        pass

    try:
        page.go_back(wait_until="domcontentloaded", timeout=_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
        if _wait_phone_form_ready(page, timeout=8):
            return True
    except Exception:
        pass

    # 直接打开 add-phone。有时 body 会短暂为空，所以 reload + wait。
    for i in range(2):
        try:
            page.goto("https://auth.openai.com/add-phone", wait_until="domcontentloaded", timeout=_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass
            if _wait_phone_form_ready(page, timeout=10):
                return True
            try:
                page.reload(wait_until="domcontentloaded", timeout=_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
            except Exception:
                pass
            if _wait_phone_form_ready(page, timeout=8):
                return True
        except Exception as exc:
            logger.info("[Codex][BrowserUse] 打开 add-phone 尝试 %s 失败：%s", i + 1, str(exc)[:160])

    logger.warning("[Codex][BrowserUse] 无法回到手机号输入页：%s", _current_state_for_log(page))
    return False

def _do_phone_verification_if_present(page) -> None:
    # 给页面一点时间从邮箱 OTP 后跳到手机号页；没有就跳过。
    end = time.time() + 20
    while time.time() < end:
        if _is_callback_url(_page_url(page)):
            return
        if _has_phone_prompt(page):
            break
        if any(x in _page_url(page).lower() for x in ("workspace", "consent", "authorize")):
            return
        time.sleep(0.5)
    if not _has_phone_prompt(page):
        logger.info("[Codex][BrowserUse] 未检测到手机号验证页，跳过")
        return

    http = sms_provider._http()
    max_retries = int(getattr(sms_provider._cfg, "SMS_MAX_RETRIES", 10) or 10) if hasattr(sms_provider, "_cfg") else 10
    last_error = ""
    for attempt in range(1, max_retries + 1):
        activation_id = None
        try:
            _t_phone_ready = _StepTimer(f"手机页准备 attempt={attempt}")
            if not _ensure_add_phone_form(page, reason=f"attempt-{attempt}"):
                raise RuntimeError("无法回到手机号输入页，暂不取新号")
            _t_phone_ready.done()
            logger.info("[Codex][BrowserUse] 需要手机验证，开始取号（%s/%s）", attempt, max_retries)
            activation_id, phone = sms_provider.acquire_number(http)
            logger.info("[Codex][BrowserUse] 已取号：%s activation=%s", phone, activation_id)
            _t_phone_send = _StepTimer(f"填写并提交手机号 attempt={attempt}")
            phone_e164 = _fill_phone(page, phone)
            _bu_delay("form")
            send_state = _wait_after_phone_send(page, timeout=12 if _fast_mode() else 18)
            _t_phone_send.done(f"state={send_state}")
            logger.info("[Codex][BrowserUse] 手机号提交后状态：%s phone=%s", send_state, phone_e164)
            if send_state == "callback":
                return
            if send_state != "code_page":
                raise RuntimeError(f"提交手机号后未确认发送短信/进入验证码页：state={send_state}, page={_current_state_for_log(page)}")
            sms_provider.set_status(activation_id, 1, http=http)
            _t_sms = _StepTimer(f"等待手机短信 attempt={attempt}")
            sms_code = sms_provider.wait_for_sms_code(activation_id, http)
            _t_sms.done()
            logger.info("[Codex][BrowserUse] 手机 OTP 收到：%s", sms_code)
            _clear_otp_inputs(page)
            _type_otp(page, sms_code)
            _bu_delay("otp_input")
            _click_first_any_frame(
                page,
                ["button[type='submit']", "button:has-text('Continue')", "button:has-text('Verify')", "button:has-text('続行')", "button:has-text('送信')", "button:has-text('继续')", "button:has-text('验证')", "form button"],
                timeout_ms=8000,
            )
            outcome = _wait_after_phone_otp(page, timeout=30)
            logger.info("[Codex][BrowserUse] 手机 OTP 提交后状态：%s", outcome)
            if outcome in ("accepted", "callback", "unknown"):
                sms_provider.complete(activation_id, http)
                return
            raise RuntimeError(f"手机验证码未通过：{outcome}")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:220]}"
            logger.warning("[Codex][BrowserUse] 手机验证失败（%s/%s）：%s", attempt, max_retries, last_error)
            if activation_id:
                try:
                    sms_provider.cancel(activation_id, http)
                except Exception:
                    pass
            if attempt >= max_retries:
                break
            try:
                _dismiss_phone_country_dropdown(page)
                _clear_phone_inputs(page)
                _ensure_add_phone_form(page, reason=f"after-fail-{attempt}")
            except Exception:
                pass
            time.sleep(min(1 + attempt, 4))
    raise RuntimeError(f"手机验证失败，已重试 {max_retries} 次：{last_error}")


def _finish_consent_workspace(context, page) -> str:
    end = time.time() + int(getattr(_roxy_cfg, "ROXY_CODEX_CALLBACK_TIMEOUT", 180) or 180)
    while time.time() < end:
        callback = _extract_callback_url_from_context(context, page)
        if callback:
            return callback
        clicked = _click_first(
            page,
            [
                "button:has-text('Select')",
                "button:has-text('Use workspace')",
                "button:has-text('Confirm')",
                "button:has-text('Authorize')",
                "button:has-text('Allow')",
                "button:has-text('Continue')",
                "button:has-text('选择')",
                "button:has-text('允许')",
                "button:has-text('继续')",
                "button[type='submit']",
            ],
            timeout_ms=2500,
        )
        if clicked:
            _bu_delay("form")
        time.sleep(0.7)
    return _wait_for_callback(context, page, timeout=5)


def _run_browser_use_codex_oauth_once(email: str, otp_provider=None, proxy: str | None = None, force: bool = False, cloud_provider: str = "browser_use") -> dict:
    from core import codex_oauth as proto
    if not force and not proto._cfg.ENABLE_CODEX_AUTO:
        return proto._codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return proto._codex_result(status="skipped", message="email 为空")
    if otp_provider is None:
        from core.email_provider import wait_for_otp as otp_provider

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return proto._codex_result(status="failed", email=email, message="缺少 playwright，请执行 pip install playwright")

    provider = str(cloud_provider or "browser_use").strip().lower()
    if provider in ("skyvern", "sv"):
        from core.skyvern_client import SkyvernClient
        provider_label = "Skyvern"
        client = SkyvernClient()
    else:
        provider_label = "BrowserUse"
        client = BrowserUseClient()

    _set_log_provider_label(provider_label)
    _t_all = _StepTimer(f"Codex {provider_label} 全流程")
    session_info = client.open_session()
    browser = None
    context = None
    page = None
    try:
        if proto._codex_auth_url_source() == "cpa":
            cpa_auth = proto._request_cpa_authorize_url()
            auth_url = cpa_auth["auth_url"]
            state = cpa_auth["state"]
            code_verifier = ""
        else:
            code_verifier, code_challenge = proto._generate_pkce()
            state = proto._generate_state()
            auth_url = proto._build_authorize_url(state, code_challenge, prompt="login")

        logger.info(
            "[Codex][%s] 开始授权：%s proxyCountry=%s profileId=%s local_proxy_arg=%s",
            provider_label,
            email,
            session_info.proxy_country_code or "-",
            session_info.profile_id or "-",
            "yes" if proxy else "no",
        )
        with sync_playwright() as p:
            _t_cdp = _StepTimer("连接 Browser Use CDP")
            connect_kwargs = {}
            if provider in ("skyvern", "sv") and hasattr(client, "cdp_headers"):
                connect_kwargs["headers"] = client.cdp_headers()
            browser = p.chromium.connect_over_cdp(session_info.connect_url, **connect_kwargs)
            _t_cdp.done()
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(_timeout_ms())
            page.set_default_navigation_timeout(_timeout_ms(getattr(_cfg, "BROWSER_USE_NAVIGATION_TIMEOUT", 90)))
            dead_tracker = _install_account_dead_response_tracker(page)

            _fill_email_and_otp(page, email, otp_provider, auth_url, dead_tracker=dead_tracker)
            _do_phone_verification_if_present(page)
            logger.info("[Codex][BrowserUse] 手机验证处理完成/无需处理，等待授权确认和 callback")
            _t_callback = _StepTimer("等待 consent/workspace/callback")
            callback_url = _finish_consent_workspace(context, page)
            _t_callback.done()
            code = proto._extract_code(callback_url, state)
            logger.info("[Codex][BrowserUse] 已捕获 callback code：%s...", code[:24])

            if proto._codex_auth_url_source() == "cpa":
                submit_payload = proto._submit_cpa_callback(callback_url)
                file_path = proto._save_cpa_local_record(
                    email=email,
                    callback_url=callback_url,
                    auth_url=auth_url,
                    state=state,
                    submit_payload=submit_payload,
                )
                msg = submit_payload.get("message") or submit_payload.get("status_message") or "CPA callback submitted"
                _t_all.done("success")
                return proto._codex_result(
                    status="success",
                    ok=True,
                    email=email,
                    file_path=str(file_path) if file_path else None,
                    callback_url=callback_url,
                    message=str(msg),
                )

            token_payload = proto._exchange_codex_token(code, code_verifier)
            storage = proto._build_codex_storage(token_payload)
            path = proto._save_codex_credential(email, storage)
            _t_all.done("success")
            return proto._codex_result(status="success", ok=True, email=email, file_path=str(path), callback_url=callback_url)
    except AccountUnusableError as exc:
        logger.warning("[Codex][BrowserUse] 账号已废：%s，%s", email, exc.error_code)
        return proto._codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code or 'account_deactivated'}）",
        )
    except Exception as exc:
        logger.error("[Codex][BrowserUse] 授权失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Codex][BrowserUse] 失败详情", exc_info=True)
        return proto._codex_result(status="failed", email=email, message=f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        keep_open = bool(getattr(_cfg, "BROWSER_USE_KEEP_BROWSER_OPEN", False))
        if provider in ("skyvern", "sv"):
            try:
                from config import skyvern as _skyvern_cfg
                keep_open = bool(getattr(_skyvern_cfg, "SKYVERN_KEEP_BROWSER_OPEN", False))
            except Exception:
                keep_open = False
        if not keep_open:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            if provider in ("skyvern", "sv") and hasattr(client, "close_browser_session") and getattr(session_info, "session_id", ""):
                try:
                    client.close_browser_session(session_info.session_id)
                except Exception:
                    pass
        _set_log_provider_label("BrowserUse")


def _has_running_asyncio_loop() -> bool:
    """当前线程如果已有 asyncio/Playwright 事件循环，不能再启动 sync_playwright。"""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        return bool(loop and loop.is_running())
    except Exception:
        return False


def _run_in_isolated_thread(fn, *args, **kwargs):
    """在独立线程执行同步 Playwright 流程。

    BrowserUse 注册流程本身已经处于 `with sync_playwright()` 内，注册成功后如果
    立刻自动跑 Codex BrowserUse，会在同一线程里再次进入 sync_playwright，从而触发：
      It looks like you are using Playwright Sync API inside the asyncio loop.

    这里复用父线程名启动子线程，保证 registration_service 的按 threadName 日志过滤
    仍能把 Codex 日志写入同一个任务日志文件。
    """
    result_box = {}
    error_box = {}
    parent_thread_name = threading.current_thread().name

    def _target():
        try:
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 需要跨线程回传
            error_box["error"] = exc

    t = threading.Thread(target=_target, name=parent_thread_name, daemon=False)
    t.start()
    t.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


def _run_browser_use_codex_oauth_impl(email: str, otp_provider=None, proxy: str | None = None, force: bool = False, cloud_provider: str = "browser_use") -> dict:
    """Browser Use Codex OAuth 入口；CPA callback 409 timeout 时重新开启一轮授权。"""
    from core import codex_oauth as proto

    max_rounds = 2
    last_result = None
    for round_no in range(1, max_rounds + 1):
        if round_no > 1:
            logger.warning(
                "[Codex][BrowserUse] CPA callback 返回 Timeout waiting for OAuth callback，重新开启第 %s/%s 轮 Codex 授权：%s",
                round_no,
                max_rounds,
                email,
            )
        result = _run_browser_use_codex_oauth_once(email=email, otp_provider=otp_provider, proxy=proxy, force=force, cloud_provider=cloud_provider)
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


def run_browser_use_codex_oauth(email: str, otp_provider=None, proxy: str | None = None, force: bool = False, cloud_provider: str = "browser_use") -> dict:
    """Browser Use Codex OAuth 入口。

    如果当前线程已经有 Playwright/asyncio loop（典型场景：BrowserUse 注册成功后
    自动触发 Codex），则切到独立线程执行，避免 sync_playwright 嵌套报错。
    """
    if _has_running_asyncio_loop():
        logger.info("[Codex][BrowserUse] 检测到当前线程已有 Playwright/asyncio loop，切换到隔离线程执行 Codex")
        return _run_in_isolated_thread(
            _run_browser_use_codex_oauth_impl,
            email=email,
            otp_provider=otp_provider,
            proxy=proxy,
            force=force,
            cloud_provider=cloud_provider,
        )
    return _run_browser_use_codex_oauth_impl(email=email, otp_provider=otp_provider, proxy=proxy, force=force, cloud_provider=cloud_provider)
