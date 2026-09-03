"""
注册/登录流程。

协议注册主路径基于手动 CDP 全量抓包从零实现：
  captures/manual_signup_full_20260816_060202_20260816_060202
浏览器注册仍走 browser_flow。
"""
import json
import base64
import hashlib
import logging
import os
import random
import re
import secrets
import subprocess
import time
import uuid
from datetime import datetime
from typing import Optional, Any
from urllib.parse import urlparse, parse_qs, parse_qsl, urljoin, urlencode, urlunparse

from .config import Config
from .contracts import MailboxProvider as MailProvider
from .http_client import browser_profiles_for_flow, create_http_session

logger = logging.getLogger(__name__)


class _FixedMailboxProvider:
    """Reuse the mailbox already claimed by a browser attempt on fallback."""

    def __init__(self, provider: MailProvider, email: str) -> None:
        self._provider = provider
        self._email = email

    def create_mailbox(self) -> str:
        return self._email

    def wait_for_otp(self, email: str, timeout: int, issued_after: float | None = None) -> str:
        return self._provider.wait_for_otp(email, timeout=timeout, issued_after=issued_after)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


class AuthResult:
    """认证结果"""

    def __init__(self):
        self.email: str = ""
        self.password: str = ""
        self.session_token: str = ""
        self.auth_session_json: str = ""
        self.access_token: str = ""
        self.device_id: str = ""
        self.csrf_token: str = ""
        self.id_token: str = ""
        self.refresh_token: str = ""
        self.cookie_header: str = ""
        self.last_email_otp_attempt: str = ""
        self.last_successful_email_otp_code: str = ""
        self.outbound_ip: str = ""
        self.outbound_ip_version: str = ""
        self.outbound_loc: str = ""
        self.proxy_trace_status: int = 0
        self.chatgpt_csrf_status: int = 0
        self.browser_impersonate: str = ""
        self.browser_user_agent: str = ""
        self.browser_sec_ch_ua_platform: str = ""
        self.browser_accept_language: str = ""
        self.browser_profile_rotated: bool = False
        self.browser_profile_id: str = ""
        self.browser_profile_json: dict[str, Any] = {}
        self.browser_traffic: dict[str, Any] = {}
        # 注册同出口预热后的试用探测结果（供入库 trial_check 优先采用）
        self.trial_probe: dict[str, Any] = {}
        # 协议注册后 TOTP 2FA（best-effort，失败不阻断账号）
        self.two_factor: dict[str, Any] = {}
        self.totp_status: str = ""
        self.totp_error: str = ""
        self.mfa_enabled: bool = False

    def is_valid(self) -> bool:
        return bool(self.session_token and self.access_token)

    def to_dict(self) -> dict:
        out = {
            "email": self.email,
            "password": self.password,
            "session_token": self.session_token,
            "auth_session_json": self.auth_session_json,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "cookie_header": self.cookie_header,
            "last_email_otp_attempt": self.last_email_otp_attempt,
            "last_successful_email_otp_code": self.last_successful_email_otp_code,
            "outbound_ip": self.outbound_ip,
            "outbound_ip_version": self.outbound_ip_version,
            "outbound_loc": self.outbound_loc,
            "proxy_trace_status": self.proxy_trace_status,
            "chatgpt_csrf_status": self.chatgpt_csrf_status,
            "browser_impersonate": self.browser_impersonate,
            "browser_user_agent": self.browser_user_agent,
            "browser_sec_ch_ua_platform": self.browser_sec_ch_ua_platform,
            "browser_accept_language": self.browser_accept_language,
            "browser_profile_rotated": self.browser_profile_rotated,
            "browser_profile_id": self.browser_profile_id,
            "browser_profile_json": self.browser_profile_json,
            "browser_traffic": self.browser_traffic,
            "trial_probe": dict(self.trial_probe or {}),
            "mfa_enabled": bool(self.mfa_enabled),
        }
        if self.totp_status:
            out["totp_status"] = self.totp_status
        if self.totp_error:
            out["totp_error"] = self.totp_error
        if self.two_factor:
            out["two_factor"] = dict(self.two_factor)
        return out


class AuthFlow:
    """注册/登录协议流"""

    def __init__(self, config: Config, sms_callback: Optional[Any] = None):
        self.config = config
        self._browser_profiles = browser_profiles_for_flow(primary=getattr(config, "browser_profile", None))
        self._profile_idx = 0
        self._browser_profile = self._browser_profiles[self._profile_idx]
        self.session = create_http_session(
            proxy=config.proxy,
            profile=self._browser_profile,
        )
        logger.info(
            "注册 HTTP 指纹: impersonate=%s ua=%s lang=%s tz=%s",
            self._browser_profile.impersonate,
            self._browser_profile.user_agent,
            getattr(self._browser_profile, "accept_language", ""),
            getattr(self._browser_profile, "timezone", ""),
        )
        self.result = AuthResult()
        self._record_browser_profile()
        # 可选 SMS 接码控制器（sms_provider.PhoneCallbackController 实例）
        # 命中 add-phone 时自动租手机号 + 接 SMS 验证码，否则回退到环境变量路径
        self._sms_callback = sms_callback
        self._http_trace_enabled = str(os.getenv("AUTH_HTTP_TRACE", "0")).lower() in ("1", "true", "yes", "on")
        # signup() 会在分支里 set；run_protocol_login 命中已有账号路径会跳过 signup，
        # 导致 kickoff_otp_delivery 读未初始化属性 AttributeError。这里给个默认值。
        self._is_existing_account = False
        self._existing_email_verification_mode = ""
        self._existing_page_type = ""
        self._manual_login_verifier = (os.getenv("LOGIN_VERIFIER", "") or "").strip()
        self._captured_login_verifier = ""
        self._oauth_client_secret = (os.getenv("OAUTH_CLIENT_SECRET", "") or "").strip()
        self._oauth_client_id = "YOUR_OPENAI_WEB_CLIENT_ID"
        self._oauth_redirect_uri = "https://chatgpt.com/api/auth/callback/openai"
        self._oauth_scope = ""
        self._oauth_state = ""
        self._oauth_auth_url = ""
        self._auth_session_logging_id = str(uuid.uuid4())
        # 与手动抓包一致：auth 文档导航 ID / access-flow invocation 贯穿 OTP+create_account
        self._document_navigation_id = str(uuid.uuid4())
        self._access_flow_invocation_id = str(uuid.uuid4())
        # chatgpt.com 客户端会话 id（oai-session-id），注册后预热/试用探测共用
        self._chatgpt_session_id = str(uuid.uuid4())
        self._client_auth_session_dump: dict[str, Any] = {}
        self._last_auth_session_json: dict[str, Any] = {}
        self._last_sentinel_token: str = ""
        self._last_sentinel_so_token: str = ""
        self._last_resend_otp_status: int = 0
        self._last_resend_otp_body: str = ""
        self._client_auth_session_id: str = ""
        self._dump_login_verifier: str = ""
        self._codex_rt_attempted: bool = False
        self._trace_dump_enabled = str(os.getenv("AUTH_TRACE_DUMP", "0")).lower() in ("1", "true", "yes", "on")
        self._trace_include_cookie = str(os.getenv("AUTH_TRACE_INCLUDE_COOKIE", "0")).lower() in (
            "1", "true", "yes", "on"
        )
        self._trace_dump_path = ""
        self._proxy_precheck_passed = False
        # Browser mode is lazy: the Playwright runtime is created only when a
        # worker actually performs a browser precheck/flow.  This keeps the
        # historical protocol mode lightweight and preserves test fixtures
        # that replace ``session`` with a small fake object.
        self._browser_session = None
        self._browser_engine = None
        self._force_protocol_mode = False

    def _browser_mode_enabled(self) -> bool:
        return (
            not bool(getattr(self, "_force_protocol_mode", False))
            and str(getattr(self.config, "registration_mode", "protocol") or "protocol").strip().lower()
            == "browser"
        )

    def _get_browser_engine(self):
        if self._browser_engine is None:
            from .browser_flow import BrowserRegistrationEngine

            self._browser_engine = BrowserRegistrationEngine(self)
        return self._browser_engine

    def close(self) -> None:
        """Close both the HTTP session and an optional browser context."""

        browser_engine = getattr(self, "_browser_engine", None)
        if browser_engine is not None:
            try:
                browser_engine.close()
            except Exception:
                logger.debug("关闭浏览器注册引擎失败", exc_info=True)
        browser_session = getattr(self, "_browser_session", None)
        if browser_session is not None and browser_engine is None:
            try:
                browser_session.close()
            except Exception:
                logger.debug("关闭浏览器 context 失败", exc_info=True)
        try:
            self.session.close()
        except Exception:
            pass

    def _record_browser_profile(self) -> None:
        self.result.browser_impersonate = self._browser_profile.impersonate
        self.result.browser_user_agent = self._browser_profile.user_agent
        self.result.browser_sec_ch_ua_platform = self._browser_profile.sec_ch_ua_platform
        self.result.browser_accept_language = self._browser_profile.accept_language
        self.result.browser_profile_id = self._browser_profile.profile_id
        try:
            self.result.browser_profile_json = self._browser_profile.to_dict()
        except Exception:
            self.result.browser_profile_json = {}

    def _iter_session_cookies(self) -> list[Any]:
        """兼容 curl_cffi / requests cookiejar 迭代。"""
        try:
            jar = getattr(self.session.cookies, "jar", None)
            if jar is not None:
                return list(jar)
        except Exception:
            pass
        try:
            return list(self.session.cookies)
        except Exception:
            return []

    def _session_cookie_names(self) -> set[str]:
        names: set[str] = set()
        for cookie in self._iter_session_cookies():
            try:
                name = (getattr(cookie, "name", "") or "").strip()
            except Exception:
                name = ""
            if name:
                names.add(name)
        # 某些 jar 迭代会丢 host-only cookie，再用 get 兜底关键名。
        for name in (
            "cf_clearance",
            "__cf_bm",
            "_cfuvid",
            "__cflb",
            "__Secure-oai-is",
            "oai-sc",
            "oai-client-auth-session",
            "oai-client-auth-info",
            "__Secure-next-auth.session-token",
        ):
            try:
                if self.session.cookies.get(name, ""):
                    names.add(name)
            except Exception:
                pass
        return names

    def _has_cf_clearance(self) -> bool:
        """是否已有 Cloudflare JS challenge 通过后的 cf_clearance。"""
        return "cf_clearance" in self._session_cookie_names()

    def _has_weak_edge_cookies(self) -> bool:
        """仅边缘 cookie（不够用于试用资格）。"""
        names = self._session_cookie_names()
        return ("__cf_bm" in names and "_cfuvid" in names) or (
            "oai-sc" in names and ("__cf_bm" in names or "_cfuvid" in names)
        )

    def _has_integrity_cookies(self) -> bool:
        """是否具备真浏览器级完整性 cookie。

        有试用号对照：几乎都有 cf_clearance。
        仅 __cf_bm/_cfuvid 不够，旧逻辑会误判“已存在”并跳过浏览器桥。
        纯协议默认不强制 cf_clearance（PROTOCOL_INTEGRITY_REQUIRE_CF_CLEARANCE=0）。
        需要浏览器级 cookie 时再显式打开 =1。
        """
        names = self._session_cookie_names()
        require_clearance = self._env_flag("PROTOCOL_INTEGRITY_REQUIRE_CF_CLEARANCE", "0")
        if require_clearance:
            return "cf_clearance" in names
        # 宽松模式：clearance / oai-is+边缘 / 仅边缘 cookie 都算可用
        if "cf_clearance" in names:
            return True
        if "__Secure-oai-is" in names and ("__cf_bm" in names or "_cfuvid" in names):
            return True
        if "__cf_bm" in names or "_cfuvid" in names or "oai-sc" in names:
            return True
        return False

    def _integrity_cookie_summary(self) -> str:
        names = sorted(self._session_cookie_names())
        interesting = [
            n
            for n in names
            if n
            in {
                "cf_clearance",
                "__cf_bm",
                "_cfuvid",
                "__cflb",
                "__Secure-oai-is",
                "oai-sc",
                "oai-client-auth-session",
                "oai-client-auth-info",
                "oai-did",
                "__Secure-next-auth.session-token",
            }
            or n.startswith("oai-")
            or n.startswith("__cf")
            or n.startswith("cf_")
        ]
        return ",".join(interesting[:24]) or "(none)"

    def _set_session_cookie(
        self,
        name: str,
        value: str,
        *,
        domain: str = ".chatgpt.com",
        path: str = "/",
    ) -> None:
        name = (name or "").strip()
        value = str(value or "")
        if not name or value == "":
            return
        domain = (domain or ".chatgpt.com").strip() or ".chatgpt.com"
        path = path or "/"
        try:
            self.session.cookies.set(name, value, domain=domain, path=path)
            return
        except TypeError:
            pass
        except Exception:
            pass
        try:
            self.session.cookies.set(name, value, domain=domain)
            return
        except Exception:
            pass
        try:
            self.session.cookies.set(name, value)
        except Exception:
            pass

    def _import_cookie_records(self, cookies: list[dict[str, Any]]) -> int:
        """把浏览器/外部 cookie 记录注入协议 session。返回成功条数。"""
        imported = 0
        for item in cookies or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name or value == "":
                continue
            domain = str(item.get("domain") or "").strip() or ".chatgpt.com"
            path = str(item.get("path") or "/") or "/"
            # 只收 chatgpt / openai 相关域，避免污染无关 cookie。
            host = domain.lstrip(".").lower()
            if not any(h in host for h in ("chatgpt.com", "openai.com", "oaistatic.com")):
                continue
            self._set_session_cookie(name, value, domain=domain, path=path)
            imported += 1
        return imported

    def _http_integrity_bootstrap(self) -> None:
        """协议侧多点预热，尽量触发 CF/OAI 边缘 cookie。"""
        headers = self._html_headers("https://chatgpt.com/")
        urls = (
            "https://chatgpt.com/",
            "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            "https://chatgpt.com/auth/login",
            "https://chatgpt.com/cdn-cgi/trace",
        )
        for url in urls:
            try:
                resp = self.session.get(url, headers=headers, timeout=25, allow_redirects=True)
                self._trace_http(f"integrity_http_{urlparse(url).path or 'home'}", resp)
            except Exception as exc:
                logger.debug("integrity HTTP 预热失败 url=%s err=%s", url[:80], exc)
        # JSON 端点也碰一下，部分部署会在此写 oai-* cookie。
        try:
            api_headers = self._common_headers("https://chatgpt.com/")
            api_headers["Accept"] = "*/*"
            for url in (
                "https://chatgpt.com/api/auth/session",
                "https://chatgpt.com/api/auth/csrf",
                "https://chatgpt.com/backend-anon/accounts/check/v4-2023-04-27?timezone_offset_min=-540",
            ):
                try:
                    resp = self.session.get(url, headers=api_headers, timeout=20, allow_redirects=True)
                    self._trace_http(f"integrity_api_{urlparse(url).path}", resp)
                except Exception:
                    continue
        except Exception:
            pass

    def _browser_integrity_warm_once(self, *, engine: str, headless: bool, profile: Any, profile_dict: dict) -> int:
        """单引擎浏览器预热一次，返回注入 cookie 数。

        目标是拿到 cf_clearance：CF JS challenge 往往需要真实浏览器 + 足够等待。
        """
        from .browser_session import BrowserSession
        from .config import Config as BrowserConfig

        warm_config = BrowserConfig(
            proxy=getattr(self.config, "proxy", None),
            browser_profile=profile_dict if isinstance(profile_dict, dict) else None,
            registration_mode="browser",
            browser_engine=engine,
            browser_headless=headless,
            # 有头预热时仍尽量藏窗，避免干扰操作台
            browser_hide_window=True,
            browser_timeout_ms=min(
                60_000,
                max(15_000, int(getattr(self.config, "browser_timeout_ms", 0) or 30_000)),
            ),
        )
        session = BrowserSession(warm_config, profile)
        imported = 0
        try:
            session.start()
            page = session.new_page(reuse_existing=True)
            timeout_ms = int(getattr(warm_config, "browser_timeout_ms", 30_000) or 30_000)
            # 首页多等一会，给 CF challenge / Turnstile 时间写 cf_clearance
            try:
                wait_clearance_ms = max(
                    3_000,
                    min(25_000, int(os.getenv("PROTOCOL_INTEGRITY_CLEARANCE_WAIT_MS", "12000") or 12000)),
                )
            except (TypeError, ValueError):
                wait_clearance_ms = 12_000

            for url in (
                "https://chatgpt.com/",
                "https://chatgpt.com/?promo_campaign=plus-1-month-free",
                "https://chatgpt.com/auth/login",
            ):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as exc:
                    logger.debug("integrity browser goto 中断 url=%s err=%s", url[:80], exc)
                # 轮询 cookie，一旦出现 cf_clearance 可提前结束等待
                deadline = time.time() + (wait_clearance_ms / 1000.0)
                names: set[str] = set()
                while time.time() < deadline:
                    try:
                        cookies = session.cookies(
                            urls=["https://chatgpt.com/", "https://chatgpt.com/auth/login"]
                        )
                    except Exception:
                        cookies = []
                    names = {
                        str(c.get("name") or "")
                        for c in (cookies or [])
                        if isinstance(c, dict)
                    }
                    if "cf_clearance" in names:
                        logger.info(
                            "integrity browser 已拿到 cf_clearance engine=%s url=%s",
                            engine,
                            url[:80],
                        )
                        break
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        time.sleep(0.5)
                if "cf_clearance" in names:
                    # 已有 clearance，导出 cookie 即可
                    break

            try:
                cookies = session.cookies(
                    urls=[
                        "https://chatgpt.com/",
                        "https://chatgpt.com/auth/login",
                        "https://auth.openai.com/",
                    ]
                )
            except Exception:
                cookies = session.cookies()
            imported = self._import_cookie_records(list(cookies or []))
            try:
                self.session.headers.update(self._browser_profile.headers())
            except Exception:
                pass
            logger.info(
                "integrity browser warm 完成 imported=%s has_cf_clearance=%s cookies=%s engine=%s headless=%s",
                imported,
                self._has_cf_clearance(),
                self._integrity_cookie_summary(),
                engine,
                headless,
            )
        finally:
            try:
                session.close()
            except Exception:
                pass
        return imported

    def _browser_integrity_warm(self) -> int:
        """短生命周期真实浏览器访问 chatgpt.com，把 CF/OAI cookie 桥回协议 session。

        不进入完整注册 UI，只做首页/登录页预热，目标是 cf_clearance。
        返回注入 cookie 条数。

        纯协议默认关闭（PROTOCOL_INTEGRITY_BROWSER_WARM=0），避免任何 Camoufox 启动。
        """
        if not self._env_flag("PROTOCOL_INTEGRITY_BROWSER_WARM", "0"):
            return 0
        # 已在浏览器注册模式里时不必再开第二套浏览器。
        if self._browser_mode_enabled() and not bool(getattr(self, "_force_protocol_mode", False)):
            return 0

        from .http_client import browser_profile_from_dict

        profile = self._browser_profile
        try:
            profile_dict = profile.to_dict()
        except Exception:
            profile_dict = getattr(self.config, "browser_profile", None) or {}
            if not isinstance(profile_dict, dict):
                profile_dict = {}
            profile = browser_profile_from_dict(profile_dict) or profile
        if not isinstance(profile_dict, dict):
            try:
                profile_dict = profile.to_dict()
            except Exception:
                profile_dict = {}

        preferred = (
            str(os.getenv("PROTOCOL_INTEGRITY_BROWSER_ENGINE", "") or "").strip().lower()
            or str(getattr(self.config, "browser_engine", "") or "").strip().lower()
            or "camoufox"
        )
        engines: list[str] = []
        # 默认只走 Camoufox：本机常缺 Playwright chromium，chrome/playwright 只会噪音失败。
        # 需要额外引擎时显式 PROTOCOL_INTEGRITY_BROWSER_FALLBACKS=playwright,chrome
        fallbacks_raw = str(os.getenv("PROTOCOL_INTEGRITY_BROWSER_FALLBACKS", "") or "").strip()
        fallbacks = [x.strip().lower() for x in fallbacks_raw.split(",") if x.strip()]
        for engine in [preferred, "camoufox", *fallbacks]:
            if engine and engine not in engines:
                engines.append(engine)
        # CF challenge 在 headless 下经常不出 cf_clearance；默认有头藏窗。
        # 需要无头时显式 PROTOCOL_INTEGRITY_HEADLESS=1。
        headless = self._env_flag("PROTOCOL_INTEGRITY_HEADLESS", "0")
        last_error = ""
        for engine in engines:
            try:
                imported = self._browser_integrity_warm_once(
                    engine=engine,
                    headless=headless,
                    profile=profile,
                    profile_dict=profile_dict,
                )
                if self._has_integrity_cookies():
                    return imported
                # 没拿到 clearance 也继续试下一引擎
                logger.warning(
                    "integrity browser warm 未拿到 cf_clearance engine=%s cookies=%s",
                    engine,
                    self._integrity_cookie_summary(),
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("integrity browser warm 失败 engine=%s err=%s", engine, last_error)
                continue
        if last_error:
            logger.warning("integrity browser warm 全部引擎失败 last=%s", last_error)
        return 0

    def ensure_integrity_cookies(self, *, stage: str = "register", required: bool | None = None) -> bool:
        """确保协议会话 cookie 可用。

        纯协议默认：
        1) 已有可用 cookie → 直接过
        2) HTTP 预热拿边缘 cookie
        3) **不**拉起浏览器（PROTOCOL_INTEGRITY_BROWSER_WARM 默认 0）

        仅当 PROTOCOL_INTEGRITY_BROWSER_WARM=1 时才走 Camoufox 桥接。
        required 默认读 PROTOCOL_INTEGRITY_REQUIRED（默认 0，不因缺 clearance 失败）。
        """
        if required is None:
            required = self._env_flag("PROTOCOL_INTEGRITY_REQUIRED", "0")
        allow_browser_warm = self._env_flag("PROTOCOL_INTEGRITY_BROWSER_WARM", "0")

        if self._has_integrity_cookies():
            logger.info(
                "integrity cookies 已存在 stage=%s has_cf_clearance=%s cookies=%s",
                stage,
                self._has_cf_clearance(),
                self._integrity_cookie_summary(),
            )
            return True

        if self._has_weak_edge_cookies() and not allow_browser_warm:
            # 纯协议：边缘 cookie 足够继续，绝不拉浏览器
            logger.info(
                "仅有 CF 边缘 cookie，纯协议继续（不启动浏览器） stage=%s cookies=%s",
                stage,
                self._integrity_cookie_summary(),
            )
            return True

        logger.info("integrity cookies 缺失，开始 HTTP 预热 stage=%s", stage)
        self._http_integrity_bootstrap()
        if self._has_integrity_cookies() or self._has_weak_edge_cookies():
            logger.info(
                "HTTP 预热后 cookie 可用 stage=%s has_cf_clearance=%s cookies=%s",
                stage,
                self._has_cf_clearance(),
                self._integrity_cookie_summary(),
            )
            if not allow_browser_warm:
                return True

        if allow_browser_warm:
            logger.info(
                "PROTOCOL_INTEGRITY_BROWSER_WARM=1，尝试浏览器桥 stage=%s cookies=%s",
                stage,
                self._integrity_cookie_summary(),
            )
            self._browser_integrity_warm()
        else:
            logger.info(
                "跳过浏览器 integrity 预热（PROTOCOL_INTEGRITY_BROWSER_WARM=0） stage=%s cookies=%s",
                stage,
                self._integrity_cookie_summary(),
            )

        ok = self._has_integrity_cookies() or self._has_weak_edge_cookies()
        logger.info(
            "integrity cookies 最终状态 stage=%s ok=%s has_cf_clearance=%s cookies=%s browser_warm=%s",
            stage,
            ok,
            self._has_cf_clearance(),
            self._integrity_cookie_summary(),
            allow_browser_warm,
        )
        if not ok and required:
            raise RuntimeError(
                f"缺少完整性 cookie（{stage}），当前={self._integrity_cookie_summary() or 'none'}；"
                "纯协议可设 PROTOCOL_INTEGRITY_REQUIRED=0 继续；"
                "若必须 cf_clearance 请设 PROTOCOL_INTEGRITY_BROWSER_WARM=1"
            )
        return ok

    def _build_chatgpt_cookie_header(self) -> str:
        """
        导出当前会话中的 chatgpt/openai 相关 cookie。

        说明：
        - `/backend-api/payments/checkout` 与试用探测不仅依赖
          `__Secure-next-auth.session-token`，还会校验 CF / OAI 完整性 cookie。
        - 因此这里尽量保留 chatgpt.com / openai.com 域 cookie 集合。
        """
        cookie_pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        for cookie in self._iter_session_cookies():
            try:
                name = (getattr(cookie, "name", "") or "").strip()
                value = getattr(cookie, "value", "") or ""
                domain = (getattr(cookie, "domain", "") or "").strip().lower()
            except Exception:
                continue
            if not name or not value:
                continue
            if domain and not any(h in domain for h in ("chatgpt.com", "openai.com")):
                continue
            if name in seen:
                continue
            seen.add(name)
            cookie_pairs.append((name, value))

        # 兜底补齐关键 cookie，避免某些 cookiejar 迭代行为差异导致遗漏
        critical_names = [
            "__Secure-next-auth.session-token",
            "__Host-next-auth.csrf-token",
            "__Secure-next-auth.callback-url",
            "oai-did",
            "oai-sc",
            "cf_clearance",
            "__cf_bm",
            "_cfuvid",
            "__cflb",
            "__Secure-oai-is",
            "oai-client-auth-session",
            "oai-client-auth-info",
            "__stripe_mid",
            "__stripe_sid",
            "oai-gn",
            "oai-nav-state",
            "oai-hlib",
            "_account_is_fedramp",
            "oai_consent_analytics",
            "oai_consent_marketing",
            "oai-allow-ne",
            "_ga",
            "_ga_9SHBSK2D9J",
            "_gcl_au",
            "_fbp",
            "_puid",
            "_dd_s",
            "g_state",
            "__oailb",
            "__obi",
            "oaicom-stable-id",
            "unified_session_manifest",
        ]
        for name in critical_names:
            if name in seen:
                continue
            try:
                value = self.session.cookies.get(name, "")
            except Exception:
                value = ""
            if value:
                seen.add(name)
                cookie_pairs.append((name, value))

        return "; ".join(f"{name}={value}" for name, value in cookie_pairs if name and value)

    def _trace_http(self, step: str, resp, extra_request: dict | None = None):
        """可选 HTTP 细粒度追踪（用于协议调试）"""
        if (not self._http_trace_enabled and not self._trace_dump_enabled) or resp is None:
            return
        try:
            req = getattr(resp, "request", None)
            method = getattr(req, "method", "") if req else ""
            req_url = getattr(req, "url", "") if req else ""
            req_body = ""
            req_headers = {}
            if req is not None:
                raw_req_body = getattr(req, "body", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "content", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "data", None)
                if isinstance(raw_req_body, bytes):
                    req_body = raw_req_body.decode("utf-8", errors="replace")
                elif raw_req_body is not None:
                    req_body = str(raw_req_body)
                try:
                    req_headers = dict(getattr(req, "headers", {}) or {})
                except Exception:
                    req_headers = {}

            # 手动补充请求信息（curl_cffi 某些场景 request.body/headers 为空）
            if isinstance(extra_request, dict):
                if not method:
                    method = str(extra_request.get("method", "") or "")
                if not req_url:
                    req_url = str(extra_request.get("url", "") or "")
                if not req_body:
                    maybe_body = extra_request.get("body", "")
                    if isinstance(maybe_body, bytes):
                        req_body = maybe_body.decode("utf-8", errors="replace")
                    else:
                        req_body = str(maybe_body or "")
                extra_headers = extra_request.get("headers", {})
                if isinstance(extra_headers, dict):
                    merged = dict(req_headers or {})
                    merged.update(extra_headers)
                    req_headers = merged

            status = getattr(resp, "status_code", "N/A")
            final_url = str(getattr(resp, "url", "") or "")
            req_cookie = (req_headers.get("Cookie", "") or "")
            location = (resp.headers.get("Location", "") or "")[:180]
            req_id = (resp.headers.get("x-request-id", "") or "")[:120]
            ctype = (resp.headers.get("Content-Type", "") or "")[:120]
            # 尽量保留完整 Set-Cookie（某些关键 cookie 可能在后续片段）
            set_cookie_list: list[str] = []
            try:
                get_list = getattr(resp.headers, "get_list", None) or getattr(resp.headers, "getlist", None)
                if callable(get_list):
                    vals = get_list("Set-Cookie")
                    if isinstance(vals, list):
                        set_cookie_list = [str(x) for x in vals if x]
            except Exception:
                set_cookie_list = []
            if not set_cookie_list:
                one = (resp.headers.get("Set-Cookie", "") or "")
                if one:
                    set_cookie_list = [one]
            set_cookie_raw = " || ".join(set_cookie_list)
            set_cookie = set_cookie_raw[:260]
            body = (resp.text or "").replace("\n", " ").replace("\r", " ")
            body = body[:260]
            req_headers_lc = {(str(k).lower()): v for k, v in (req_headers or {}).items()}

            if self._http_trace_enabled:
                logger.info(
                    "[HTTP TRACE] %s | %s %s -> %s | url=%s | location=%s | req_id=%s | ctype=%s | set_cookie=%s | body=%s",
                    step,
                    method,
                    req_url[:180],
                    status,
                    final_url[:180],
                    location,
                    req_id,
                    ctype,
                    set_cookie,
                    body,
                )
                if self._trace_include_cookie and req_cookie:
                    logger.info("[HTTP TRACE] %s | req_cookie=%s", step, req_cookie[:360])

            # 从多处信息中抓取 login_verifier/code_verifier
            self._sniff_login_verifier(req_url, f"{step}:req_url")
            self._sniff_login_verifier(req_body, f"{step}:req_body")
            self._sniff_login_verifier(final_url, f"{step}:final_url")
            self._sniff_login_verifier(location, f"{step}:location")
            raw_text = resp.text or ""
            self._sniff_login_verifier(raw_text, f"{step}:resp_body")

            # 明文 HTTP 抓包落盘（jsonl）
            if self._trace_dump_enabled and self._trace_dump_path:
                try:
                    include_req_cookie = self._env_flag("AUTH_TRACE_INCLUDE_REQ_COOKIE", "0")
                    record = {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "step": step,
                        "request": {
                            "method": method,
                            "url": req_url,
                            "body": req_body[:120000],
                            "headers": {
                                "Content-Type": (req_headers_lc.get("content-type", "") or "")[:240],
                                "Accept": (req_headers_lc.get("accept", "") or "")[:240],
                                "Referer": (req_headers_lc.get("referer", "") or "")[:500],
                                "Origin": (req_headers_lc.get("origin", "") or "")[:120],
                                **(
                                    {
                                        "Cookie": (req_headers_lc.get("cookie", "") or "")[:6000],
                                    }
                                    if include_req_cookie
                                    else {}
                                ),
                            },
                        },
                        "response": {
                            "status_code": status,
                            "url": final_url,
                            "location": resp.headers.get("Location", ""),
                            "x_request_id": resp.headers.get("x-request-id", ""),
                            "content_type": resp.headers.get("Content-Type", ""),
                            "set_cookie": set_cookie_raw,
                            "set_cookie_list": set_cookie_list,
                            "body": raw_text[:120000],
                        },
                        "captured_login_verifier": self._captured_login_verifier,
                    }
                    if self._trace_include_cookie and req_cookie:
                        record["request"]["headers"]["Cookie"] = req_cookie[:8000]
                    with open(self._trace_dump_path, "a", encoding="utf-8") as fw:
                        fw.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.debug(f"HTTP 抓包写入失败: {e}")
        except Exception as e:
            logger.debug(f"HTTP trace 输出失败: {e}")

    def _sniff_login_verifier(self, text: str, source: str = ""):
        """从任意文本中提取 login_verifier/code_verifier。"""
        if not text:
            return
        try:
            patterns = [
                r"(?:login_verifier|code_verifier|verifier)=([A-Za-z0-9._~-]{8,})",
                r'"(?:login_verifier|code_verifier|verifier)"\s*:\s*"([^"]{8,})"',
            ]
            for p in patterns:
                m = re.search(p, text)
                if not m:
                    continue
                v = (m.group(1) or "").strip()
                if not v:
                    continue
                if v != self._captured_login_verifier:
                    self._captured_login_verifier = v
                    logger.info("捕获 login_verifier 来源=%s len=%s", source or "unknown", len(v))
                return
        except Exception:
            return

    @staticmethod
    def _walk_collect_str_fields(obj: Any, wanted_keys: set[str], out: dict[str, str], depth: int = 0, max_depth: int = 6):
        """递归收集目标字段（仅字符串值）。"""
        if depth > max_depth or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kk = (str(k) or "").strip().lower()
                if kk in wanted_keys and isinstance(v, str) and v.strip():
                    out[kk] = v.strip()
                AuthFlow._walk_collect_str_fields(v, wanted_keys, out, depth + 1, max_depth)
        elif isinstance(obj, list):
            for it in obj:
                AuthFlow._walk_collect_str_fields(it, wanted_keys, out, depth + 1, max_depth)

    def fetch_client_auth_session_dump(self, stage: str = "") -> dict:
        """
        尝试读取 auth.openai 的 client_auth_session_dump：
        - 可能包含 session_id / client_auth_session 的额外状态
        - 若出现 verifier/refresh 相关字段，自动注入当前流程
        """
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Accept"] = "application/json"
        try:
            resp = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers=headers,
                timeout=30,
            )
            self._trace_http(f"client_auth_session_dump_{stage or 'default'}", resp)
        except Exception as e:
            logger.debug(f"client_auth_session_dump 请求异常({stage}): {e}")
            return {}

        if resp.status_code != 200:
            logger.info(
                "client_auth_session_dump(%s) 非 200: %s",
                stage or "default",
                resp.status_code,
            )
            return {}

        try:
            data = resp.json()
        except Exception:
            logger.warning(f"client_auth_session_dump({stage}) JSON 解析失败")
            return {}

        if not isinstance(data, dict):
            return {}

        self._client_auth_session_dump = data
        cas = data.get("client_auth_session", {}) if isinstance(data.get("client_auth_session"), dict) else {}

        sid = (data.get("session_id", "") or "").strip() or (cas.get("session_id", "") or "").strip()
        if sid:
            self._client_auth_session_id = sid

        # 同步 OAuth client_id（若 dump 给出更准确值）
        dump_client_id = (cas.get("openai_client_id", "") or data.get("openai_client_id", "") or "").strip()
        if dump_client_id:
            self._oauth_client_id = dump_client_id

        wanted = {
            "login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier",
            "refresh_token", "oauth_refresh_token", "access_token", "id_token",
        }
        found: dict[str, str] = {}
        self._walk_collect_str_fields(data, wanted, found)

        # verifier 候选
        for key in ("login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier"):
            v = (found.get(key, "") or "").strip()
            if v and len(v) >= 8:
                self._dump_login_verifier = v
                self._captured_login_verifier = v
                logger.info("client_auth_session_dump 捕获 verifier: key=%s len=%s", key, len(v))
                break

        # token 候选（极少见，但若有直接收下）
        refresh = (found.get("refresh_token", "") or found.get("oauth_refresh_token", "")).strip()
        if refresh:
            self.result.refresh_token = refresh
        acc = (found.get("access_token", "") or "").strip()
        if acc:
            self.result.access_token = acc
        idt = (found.get("id_token", "") or "").strip()
        if idt:
            self.result.id_token = idt

        logger.info(
            "client_auth_session_dump(%s) 成功: top_keys=%s cas_keys=%s session_id=%s refresh=%s verifier=%s",
            stage or "default",
            list(data.keys())[:12],
            list(cas.keys())[:18] if isinstance(cas, dict) else [],
            (self._client_auth_session_id[:24] if self._client_auth_session_id else ""),
            "有" if self.result.refresh_token else "无",
            "有" if self._dump_login_verifier else "无",
        )
        return data

    @staticmethod
    def _is_tls_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = ["curl: (35)", "tls connect error", "openssl_internal", "sslerror"]
        return any(m in msg for m in markers)

    @staticmethod
    def _is_registration_disallowed_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "registration_disallowed" in msg

    def _get_cookie_value_by_name(self, name: str) -> str:
        """按 cookie 名称获取值（忽略 domain 冲突）。"""
        try:
            jar = getattr(self.session.cookies, "jar", None)
            if jar is None:
                return ""
            target = (name or "").strip().lower()
            for c in jar:
                if (getattr(c, "name", "") or "").strip().lower() == target:
                    return (getattr(c, "value", "") or "").strip()
        except Exception:
            pass
        return ""

    def _extract_login_challenge_from_cookie(self) -> str:
        """
        从 login_session cookie 中提取 login_challenge。
        login_session 的第一段通常是 base64url(JSON)。
        """
        raw = self._get_cookie_value_by_name("login_session")
        if not raw:
            return ""
        try:
            p0 = raw.split(".")[0]
            p0 += "=" * (-len(p0) % 4)
            payload = json.loads(base64.urlsafe_b64decode(p0.encode("utf-8")).decode("utf-8"))
            return (payload.get("login_challenge", "") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_query_first(url: str, keys: list[str]) -> str:
        if not url:
            return ""
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:
            return ""
        for k in keys:
            val = qs.get(k, [None])[0]
            if val:
                return val
        return ""

    @staticmethod
    def _extract_page_type(resp_json: dict | None) -> str:
        if not isinstance(resp_json, dict):
            return ""
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        return (page.get("type", "") or "").strip()

    @staticmethod
    def _extract_continue_url_from_step(resp_json: dict | None) -> str:
        """
        从 auth step 响应提取 continue_url：
        - 顶层 continue_url
        - page.type=external_url 时 payload.url
        """
        if not isinstance(resp_json, dict):
            return ""
        continue_url = (resp_json.get("continue_url", "") or "").strip()
        if continue_url:
            return continue_url
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        if (page.get("type", "") or "").strip() != "external_url":
            return ""
        payload = page.get("payload", {})
        if not isinstance(payload, dict):
            return ""
        return (payload.get("url", "") or "").strip()

    @staticmethod
    def _env_flag(name: str, default: str = "0") -> bool:
        return str(os.getenv(name, default)).lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _b64url_no_pad(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    def _remember_oauth_params(self, auth_url: str):
        """从 authorize URL 记住 OAuth 参数，供后续 token exchange 使用。"""
        if not auth_url:
            return
        self._oauth_auth_url = auth_url
        try:
            qs = parse_qs(urlparse(auth_url).query)
            self._oauth_client_id = (qs.get("client_id", [self._oauth_client_id])[0] or self._oauth_client_id).strip()
            self._oauth_redirect_uri = (
                qs.get("redirect_uri", [self._oauth_redirect_uri])[0] or self._oauth_redirect_uri
            ).strip()
            self._oauth_scope = (qs.get("scope", [""])[0] or "").strip()
            self._oauth_state = (qs.get("state", [""])[0] or "").strip()
        except Exception:
            return

    def _build_pkce_pair(self, raw_bytes: int = 64) -> tuple[str, str]:
        """生成 (code_verifier, code_challenge)。"""
        verifier = self._b64url_no_pad(secrets.token_bytes(max(32, int(raw_bytes))))
        if len(verifier) < 43:
            verifier = (verifier + ("A" * 43))[:43]
        if len(verifier) > 128:
            verifier = verifier[:128]
        challenge = self._b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
        return verifier, challenge

    def _build_codex_authorize(self, prompt_override: Optional[str] = None) -> tuple[str, str, str, str, str]:
        """
        构建用于获取 refresh_token 的 Codex OAuth 授权 URL。
        参考 any-auto-register 的实现：独立 client_id + redirect_uri + 可控 PKCE。
        """
        client_id = (os.getenv("OAUTH_CODEX_CLIENT_ID", "") or "").strip() or "app_EMoamEEZ73f0CkXaXp7hrann"
        redirect_uri = (os.getenv("OAUTH_CODEX_REDIRECT_URI", "") or "").strip() or "http://localhost:1455/auth/callback"
        scope = (os.getenv("OAUTH_CODEX_SCOPE", "") or "").strip() or "openid email profile offline_access"
        state = self._b64url_no_pad(secrets.token_bytes(24))
        verifier, challenge = self._build_pkce_pair()
        prompt = (
            (os.getenv("OAUTH_CODEX_PROMPT", "login") or "").strip()
            if prompt_override is None
            else (prompt_override or "").strip()
        )
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        if prompt:
            params["prompt"] = prompt
        auth_url = f"https://auth.openai.com/oauth/authorize?{urlencode(params)}"
        return auth_url, state, verifier, redirect_uri, client_id

    @staticmethod
    def _callback_has_code(url: str, redirect_uri: str) -> bool:
        if not url:
            return False
        try:
            cb_base = (redirect_uri or "").split("?", 1)[0].rstrip("/")
            target = url.split("?", 1)[0].rstrip("/")
            if cb_base and target == cb_base:
                qs = parse_qs(urlparse(url).query)
                return bool((qs.get("code", [""])[0] or "").strip())
        except Exception:
            return False
        return False

    def _follow_authorize_for_callback(self, start_url: str, redirect_uri: str, trace_prefix: str) -> tuple[str, str]:
        """
        跟随 auth.openai.com 授权链路，捕获 callback（不消费 callback）。
        返回 (callback_url, final_url)。
        """
        current = start_url
        callback_url = ""
        chose_account = False  # /choose-an-account 每条链路只选一次，防 200/同 URL 循环
        for i in range(12):
            if self._callback_has_code(current, redirect_uri):
                callback_url = current
                break
            resp = self.session.get(
                current,
                headers=self._html_headers("https://chatgpt.com/"),
                timeout=30,
                allow_redirects=False,
            )
            self._trace_http(f"{trace_prefix}_hop_{i+1}", resp)

            # workspace/consent 页面 200 时，主动选择 workspace，拿下一跳 continue_url
            if resp.status_code == 200:
                is_workspace_like = (
                    ("/workspace" in current)
                    or ("/sign-in-with-chatgpt/" in current)
                    or ("/consent" in current)
                )
                if is_workspace_like:
                    workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                    if workspace_id:
                        next_url = self._workspace_select(workspace_id)
                        if next_url:
                            if next_url.startswith("/"):
                                next_url = urljoin("https://auth.openai.com", next_url)
                            current = next_url
                            continue

                # /choose-an-account：OpenAI 已登录多账号的选择页（react-router SSR）。
                # HTML 里 streamController.enqueue 注入 unified_sessions[].id (us_*) 和
                # authsess_*。protocol 端要主动选第一个 us_*，否则 codex callback 拿不到。
                if "/choose-an-account" in current and not chose_account:
                    chose_account = True
                    next_url = self._choose_account_select(resp.text or "", current)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current = next_url
                        continue

            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = (resp.headers.get("Location", "") or "").strip()
            if not loc:
                break
            if loc.startswith("/"):
                loc = urljoin(current, loc)
            if self._callback_has_code(loc, redirect_uri):
                callback_url = loc
                current = loc
                break
            current = loc
        return callback_url, current

    @staticmethod
    def _drop_query_keys(url: str, drop_keys: set[str]) -> str:
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            params = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for (k, v) in params if (k or "").strip() not in drop_keys]
            return urlunparse(parsed._replace(query=urlencode(kept)))
        except Exception:
            return url

    def _exchange_codex_callback_code(
        self,
        callback_url: str,
        expected_state: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
    ) -> bool:
        qs = parse_qs(urlparse(callback_url).query)
        code = (qs.get("code", [""])[0] or "").strip()
        got_state = (qs.get("state", [""])[0] or "").strip()
        if not code:
            logger.warning("Codex callback 缺少 code")
            return False
        if expected_state and got_state and got_state != expected_state:
            logger.warning("Codex callback state 不匹配，期望=%s 实际=%s", expected_state[:20], got_state[:20])
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        headers.update(self._browser_profile.headers())
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        encoded_form = urlencode(form)
        resp = self.session.post(
            "https://auth.openai.com/oauth/token",
            headers=headers,
            data=encoded_form,
            timeout=30,
        )
        self._trace_http(
            "oauth_token_exchange_codex_pkce",
            resp,
            extra_request={
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            },
        )
        if resp.status_code != 200:
            logger.warning("Codex oauth/token 失败: %s - %s", resp.status_code, (resp.text or "")[:220])
            return False
        data = resp.json() if resp is not None else {}
        self.result.id_token = data.get("id_token", self.result.id_token)
        self.result.access_token = data.get("access_token", self.result.access_token)
        self.result.refresh_token = data.get("refresh_token", self.result.refresh_token)
        logger.info(
            "Codex OAuth 交换成功: access=%s refresh=%s",
            "有" if self.result.access_token else "无",
            "有" if self.result.refresh_token else "无",
        )
        return True

    def _codex_drive_login_from_log_in(self, mail_provider: Optional[MailProvider] = None) -> str:
        """
        当 Codex 授权回落到 /log-in 时，补走一次纯协议登录推进状态机。
        返回可继续跟随的 continue_url（若无则返回空字符串）。
        """
        email = (self.result.email or "").strip()
        if not email:
            logger.warning("Codex 登录推进缺少 email")
            return ""
        password = (self.result.password or "").strip() or self._default_password_from_email(email)
        self.result.password = password

        device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
        if not device_id:
            device_id = str(uuid.uuid4())
            self.result.device_id = device_id

        sentinel = self.get_sentinel_token(device_id)
        step = self.authorize_continue(
            email=email,
            sentinel_token=sentinel,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="authorize_continue_login_codex",
        )
        page_type = self._extract_page_type(step)
        continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        if page_type == "login_password" or "/log-in/password" in continue_url:
            step = self.login_password_verify(password)
            page_type = self._extract_page_type(step)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        need_otp = (page_type == "email_otp_verification") or ("/email-verification" in (continue_url or ""))
        if need_otp:
            if mail_provider is None:
                logger.warning("Codex 登录推进需要 OTP，但未提供 mail_provider")
                return continue_url or ""
            try:
                otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180
            otp_sent_at = time.time()
            if not self.kickoff_otp_delivery("codex_login_need_otp"):
                self.send_otp()
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            otp_resp = self.verify_otp(otp_code)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(otp_resp))

        # add-phone 分支（可选）：
        # 仅在配置了手机号与验证码获取方式时尝试自动推进
        if self._is_add_phone_state(page_type="", continue_url=continue_url):
            next_url = self._handle_add_phone_verification(continue_url=continue_url)
            if next_url:
                continue_url = self._normalize_continue_url(next_url)

        return continue_url or ""

    @staticmethod
    def _is_add_phone_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "add_phone") or ("add-phone" in cu)

    def _phone_headers(self, referer: str) -> dict:
        headers = self._common_headers(referer)
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://auth.openai.com"
        device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
        if device_id:
            headers["oai-device-id"] = device_id
        return headers

    def _add_phone_send(self, phone_number: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/add-phone")
        try:
            resp = self.session.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                headers=headers,
                json={"phone_number": phone_number},
                timeout=30,
            )
        except Exception as e:
            logger.warning("[add-phone] 网络异常: %s (phone=%s)", e, phone_number)
            raise
        self._trace_http("add_phone_send", resp)

        if resp.status_code != 200:
            # 解析 error.message（如果有的话）
            try:
                data = resp.json()
                msg = data.get("error", {}).get("message", "")
                code = data.get("error", {}).get("code", "")
            except Exception:
                msg = resp.text[:150]
                code = ""
            # 抛异常时只带 message（不带完整 JSON），让上层日志更简洁
            raise RuntimeError(msg or f"HTTP {resp.status_code}")

        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def _phone_otp_resend(self) -> bool:
        headers = self._phone_headers("https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("phone_otp_resend", resp)
        return resp.status_code == 200

    def _phone_otp_validate(self, code: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            headers=headers,
            json={"code": code},
            timeout=30,
        )
        self._trace_http("phone_otp_validate", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"phone-otp/validate 失败: {resp.status_code} - {(resp.text or '')[:220]}")
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _extract_otp6(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        return (m.group(1) if m else "").strip()

    def _read_phone_otp_from_cmd(self) -> str:
        """
        从环境变量 OPENAI_PHONE_OTP_CMD 指定的命令读取手机验证码（stdout）。
        命令输出中只要出现 6 位数字即视为命中。
        """
        cmd = (os.getenv("OPENAI_PHONE_OTP_CMD", "") or "").strip()
        if not cmd:
            return ""
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, timeout=20)
            return self._extract_otp6(out or "")
        except Exception:
            return ""

    def _wait_phone_otp(self, timeout: int = 180) -> str:
        static_otp = self._extract_otp6(os.getenv("OPENAI_PHONE_OTP", "") or "")
        if static_otp:
            return static_otp

        deadline = time.time() + max(20, int(timeout))
        while time.time() < deadline:
            code = self._read_phone_otp_from_cmd()
            if code:
                return code
            time.sleep(4)
        raise TimeoutError(f"等待手机 OTP 超时 ({timeout}s)")

    def _handle_add_phone_verification(self, continue_url: str = "") -> str:
        """
        处理 add-phone 验证分支：
        - 优先使用 self._sms_callback（SMS 接码 controller，自动租号 + 接码）
        - 回退到环境变量路径：OPENAI_PHONE_NUMBER + OPENAI_PHONE_OTP_CMD/OPENAI_PHONE_OTP
        """
        if self._sms_callback is not None:
            try:
                return self._handle_add_phone_via_sms(continue_url)
            except Exception as e:
                logger.warning("SMS 接码流程失败，回退环境变量路径: %s", e)
                try:
                    self._sms_callback.cleanup()
                except Exception:
                    pass
        return self._handle_add_phone_via_env(continue_url)

    def _handle_add_phone_via_sms(self, continue_url: str = "") -> str:
        """走 SMS 接码 controller：租号 → add-phone/send → 等 SMS → validate。

        支持平台：SmsBower（smsbower.page）。
        单号窗口 80s（每 20s × 3 触发一次 OpenAI 端 resend）；失败自动 cancel + 换新号。
        最多换号次数默认 3，主人可在 WebUI / 环境变量 OPENAI_PHONE_MAX_ATTEMPTS 自定义。
        """
        ctrl = self._sms_callback
        try:
            ctrl.set_resend_callback(self._phone_otp_resend)
        except Exception:
            pass

        # 用 try/finally 保证即使 for 循环抛异常，也能 release lock + 最后一次 cleanup
        try:
            return self._do_sms_loop(ctrl)
        finally:
            # 无论成败都释放 lock + cleanup 最后一个号（如果有）
            try:
                ctrl.cleanup()
            except Exception:
                pass
            try:
                ctrl._release_lock()
            except Exception:
                pass

    def _do_sms_loop(self, ctrl) -> str:
        """SMS 接码循环逻辑（for 0..max_attempts）。"""
        # provider 信息（目前只支持 SmsBower）
        provider_key = (getattr(ctrl, "provider_key", "") or "").lower()

        # 优先从 controller.config 读（前端配置） → 环境变量兜底 → 用默认
        ctrl_cfg = getattr(ctrl, "config", None) or {}

        def _read_int(cfg_key: str, env_key: str, default: str, min_v: int = 1) -> int:
            raw = (str(ctrl_cfg.get(cfg_key) or "")).strip()
            if not raw:
                raw = os.getenv(env_key, default)
            try:
                return max(min_v, int(raw))
            except Exception:
                return int(default)

        # 单号等待窗口（秒）：默认 80 = 20×3 + 20 缓冲
        per_phone_timeout = max(40, _read_int(
            "sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT", "80", min_v=40
        ))
        # 最多换几个号（默认 3）
        max_phone_attempts = _read_int(
            "sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS", "3"
        )
        # 单号内 code validate 失败后的重试次数（如果还有时间）
        max_code_retries_per_phone = _read_int(
            "sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES", "2"
        )

        logger.info(
            "[sms] 配置: provider=%s 单号窗口=%ds 最多换号=%d 单号内验证重试=%d",
            provider_key, per_phone_timeout, max_phone_attempts, max_code_retries_per_phone,
        )

        # OpenAI "号已被使用 / 不允许" 类错误关键字
        _PHONE_REJECTED_PATTERNS = (
            "phone_number_already_in_use", "already_in_use", "already_taken",
            "phone_already_verified", "already_verified",
            "disallowed_phone", "invalid_phone_number", "phone_number_invalid",
            "blocked_phone", "phone_number_blocked",
            "suspicious behavior from phone",  # OpenAI 风控：号段可疑
        )
        def _is_phone_rejected(s: str) -> bool:
            sl = (s or "").lower()
            return any(p in sl for p in _PHONE_REJECTED_PATTERNS)

        last_err: Optional[Exception] = None

        for phone_attempt in range(1, max_phone_attempts + 1):
            logger.info("[sms] 🔁 第 %d/%d 个号尝试...", phone_attempt, max_phone_attempts)

            # 阶段 1：租号（第 2+ 个号会自动租新号，SmsBower cache 已被前一次 cleanup 清掉）
            try:
                phone = ctrl.get_phone()
            except Exception as e:
                last_err = e
                logger.warning("[sms] 第 %d 个号租号失败: %s", phone_attempt, e)
                continue
            if not phone:
                last_err = RuntimeError("SMS 接码 controller 未返回手机号")
                continue

            # 阶段 2：通知 OpenAI 发码到这个号
            send_resp = None
            try:
                logger.info("[sms] 📤 准备 POST add-phone/send (phone=%s) ...", phone)
                send_resp = self._add_phone_send(phone)
                logger.info("[sms] ✅ POST add-phone/send 成功 (phone=%s)", phone)
            except Exception as e:
                err_text = str(e)
                if "too many phone verification" in err_text.lower() \
                        or "phone_verification_rate_limit" in err_text.lower():
                    logger.warning(
                        "⚠️ OpenAI 频控: 这个 outlook 号/IP 已累积太多 add-phone 请求，"
                        "建议换 outlook 号或换代理 IP 后重试。本次放弃 add-phone（session_token 仍可保留）"
                    )
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    break
                if _is_phone_rejected(err_text):
                    logger.warning("[sms] 号 %s 被 OpenAI 拒（已用过/不允许）: %s",
                                   phone, err_text[:200])
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    continue
                # 其它未识别错误 → 也打详细日志但不视为"号码问题"
                logger.warning("[sms] 号 %s POST add-phone/send 失败（未识别错误）: %s",
                               phone, err_text[:300])
                ctrl.mark_send_failed(err_text)
                last_err = e
                continue

            send_page_type = self._extract_page_type(send_resp)
            send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
            if send_page_type not in ("phone_otp_verification", "external_url") \
                    and "phone-verification" not in (send_continue or ""):
                logger.warning(
                    "add-phone/send 未进入手机验证码页: page=%s continue=%s",
                    send_page_type or "(empty)",
                    (send_continue or "")[:180],
                )
                ctrl.mark_send_failed("did not enter phone-verification page")
                last_err = RuntimeError(f"add-phone/send 未进入 phone-verification: page={send_page_type}")
                continue

            ctrl.mark_send_succeeded()

            # 阶段 3：等 SMS code（SmsBower 内部会按 20s × 3 调 OpenAI resend）
            phone_start = time.time()
            seen_codes: set[str] = set()
            code_attempt = 0
            phone_used = False

            while time.time() - phone_start < per_phone_timeout and code_attempt < max_code_retries_per_phone:
                remaining = per_phone_timeout - (time.time() - phone_start)
                if remaining < 10:
                    break
                code_attempt += 1
                logger.info(
                    "[sms] 号 %s 第 %d/%d 次等 SMS (剩余 %ds)",
                    phone, code_attempt, max_code_retries_per_phone, int(remaining),
                )
                code = ctrl.get_code(timeout=int(remaining))
                if not code:
                    break  # 超时换号
                if code in seen_codes:
                    logger.warning("[sms] 收到重复 code=%s，跳过", code)
                    continue
                seen_codes.add(code)
                phone_used = True

                try:
                    validate_resp = self._phone_otp_validate(code)
                    next_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(validate_resp)
                    )
                    logger.info("[sms] ✅ phone-otp/validate 通过 (phone=%s code=%s) next=%s",
                                phone, code, (next_url or "")[:160])
                    ctrl.report_success()
                    return next_url or continue_url or ""
                except Exception as e:
                    last_err = e
                    err_text = str(e)
                    logger.warning("[sms] validate 失败 (phone=%s code=%s): %s",
                                   phone, code, err_text[:200])
                    ctrl.mark_code_failed(err_text)
                    # 继续 while 循环等下一条 code（同号）

            # 单号窗口结束：cancel 这个号
            logger.warning("[sms] 号 %s 已用尽 %ds 窗口", phone, per_phone_timeout)
            try:
                ctrl.cleanup()
            except Exception:
                pass
            # cleanup 清掉 controller.activation，下一轮 get_phone 会租新号

        # 所有号都失败
        if last_err:
            raise last_err
        raise RuntimeError(f"SMS 接码 {max_phone_attempts} 个号均失败")

    def _handle_add_phone_via_env(self, continue_url: str = "") -> str:
        """
        处理 add-phone 验证分支（环境变量路径，旧用法）：
        - 需要通过环境变量提供号码与验证码来源：
          - OPENAI_PHONE_NUMBER=+1...
          - OPENAI_PHONE_OTP_CMD='...返回短信内容...' 或 OPENAI_PHONE_OTP=123456
        """
        phone_raw = (os.getenv("OPENAI_PHONE_NUMBER", "") or "").strip()
        phone_candidates = [x.strip() for x in phone_raw.split(",") if x.strip()]
        if not phone_candidates:
            logger.warning("命中 add-phone，但未配置 SMS 接码 / OPENAI_PHONE_NUMBER，无法继续推进")
            return continue_url or ""

        try:
            otp_timeout = max(30, int(os.getenv("OPENAI_PHONE_OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        last_err = ""
        for idx, phone in enumerate(phone_candidates, 1):
            try:
                logger.info("add-phone 尝试号码 %s/%s: %s", idx, len(phone_candidates), phone)
                send_resp = self._add_phone_send(phone)
                send_page_type = self._extract_page_type(send_resp)
                send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
                if send_page_type not in ("phone_otp_verification", "external_url") and "phone-verification" not in (send_continue or ""):
                    logger.warning(
                        "add-phone/send 未进入手机验证码页: page=%s continue=%s",
                        send_page_type or "(empty)",
                        (send_continue or "")[:180],
                    )
                    continue

                phone_code = self._wait_phone_otp(timeout=otp_timeout)
                validate_resp = self._phone_otp_validate(phone_code)
                next_url = self._normalize_continue_url(self._extract_continue_url_from_step(validate_resp))
                logger.info("add-phone 验证通过，next=%s", (next_url or "")[:180])
                return next_url or continue_url or ""
            except Exception as e:
                last_err = str(e)
                logger.warning("add-phone 号码 %s 失败: %s", phone, e)
                try:
                    self._phone_otp_resend()
                except Exception:
                    pass

        if last_err:
            logger.warning("add-phone 阶段未成功: %s", last_err)
        return continue_url or ""

    def _codex_refresh_retry_after_add_phone(
        self,
        auth_url: str,
        redirect_uri: str,
        attempts: int = 3,
        sleep_seconds: float = 1.2,
    ) -> tuple[str, str]:
        """
        当命中 add-phone 时，按“刷新重试”策略重复发起 authorize，
        期望命中不需要 add-phone 的分支并直接拿 callback code。
        """
        callback_url = ""
        final_url = ""
        start_url = self._drop_query_keys(auth_url, {"prompt"}) or auth_url
        rounds = max(1, int(attempts))
        wait_s = max(0.0, float(sleep_seconds))

        for i in range(rounds):
            callback_url, final_url = self._follow_authorize_for_callback(
                start_url,
                redirect_uri,
                f"codex_add_phone_refresh_retry_{i+1}",
            )
            if callback_url:
                return callback_url, final_url
            if i < rounds - 1 and wait_s > 0:
                time.sleep(wait_s)

        return callback_url, final_url

    def oauth_codex_rt_exchange(self, mail_provider: Optional[MailProvider] = None) -> bool:
        """
        纯协议方式获取 RT（参考 any-auto-register）：
        - 使用独立 Codex OAuth 参数重新授权（可控 PKCE）
        - 捕获 callback code（不消费）
        - 直接调 /oauth/token 交换 access_token + refresh_token
        """
        allow_retry = self._env_flag("OAUTH_CODEX_RT_ALLOW_RETRY", "0")
        if self._codex_rt_attempted and (not allow_retry):
            logger.info("Codex RT 本轮已尝试过，跳过重复尝试（可用 OAUTH_CODEX_RT_ALLOW_RETRY=1 强制重试）")
            return False
        self._codex_rt_attempted = True

        logger.info("尝试 Codex OAuth 直连换取 refresh_token ...")
        try:
            auth_url, state, verifier, redirect_uri, client_id = self._build_codex_authorize()
            self._oauth_auth_url = auth_url
            self._oauth_client_id = client_id
            self._oauth_redirect_uri = redirect_uri
            self._oauth_state = state
            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            callback_url, final_url = self._follow_authorize_for_callback(
                auth_url, redirect_uri, "codex_authorize"
            )

            # 若被打回 /log-in，补走一次协议登录，再继续授权链路
            if (not callback_url) and "/log-in" in (final_url or ""):
                logger.info("Codex 授权回落到 /log-in，尝试协议推进登录状态...")
                continue_url = ""
                try:
                    continue_url = self._codex_drive_login_from_log_in(mail_provider=mail_provider)
                except Exception as e:
                    logger.warning(f"Codex 登录推进失败，改走 no-prompt 兜底: {e}")
                if continue_url:
                    # 命中 add-phone 时，支持“刷新重试”策略（不立刻放弃）
                    if self._is_add_phone_state(page_type="", continue_url=continue_url) and self._env_flag(
                        "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY", "1"
                    ):
                        try:
                            retry_count = max(1, int(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY_COUNT", "3")))
                        except Exception:
                            retry_count = 3
                        try:
                            retry_sleep = max(0.0, float(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_SLEEP", "1.2")))
                        except Exception:
                            retry_sleep = 1.2
                        logger.info("命中 add-phone，执行 authorize 刷新重试: count=%s sleep=%.1fs", retry_count, retry_sleep)
                        callback_url, final_url = self._codex_refresh_retry_after_add_phone(
                            auth_url=auth_url,
                            redirect_uri=redirect_uri,
                            attempts=retry_count,
                            sleep_seconds=retry_sleep,
                        )
                    else:
                        callback_url, final_url = self._follow_authorize_for_callback(
                            continue_url,
                            redirect_uri,
                            "codex_post_login",
                        )

            # Codex authorize 直接被打到 /add-phone（不经过 /log-in）：
            # 如果配了 SMS 接码 controller，先把手机号绑了再重新 authorize
            if (not callback_url) and self._is_add_phone_state(page_type="", continue_url=final_url or "") \
                    and self._sms_callback is not None:
                logger.info("Codex 授权直接落到 /add-phone，尝试 SMS 接码绑号 ...")
                try:
                    self._handle_add_phone_via_sms(continue_url=final_url)
                    # 绑号成功后重新 authorize 拿 callback code
                    callback_url, final_url = self._follow_authorize_for_callback(
                        auth_url, redirect_uri, "codex_authorize_after_add_phone"
                    )
                    if not callback_url:
                        no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                        if no_prompt_url and no_prompt_url != auth_url:
                            callback_url, final_url = self._follow_authorize_for_callback(
                                no_prompt_url,
                                redirect_uri,
                                "codex_authorize_noprompt_after_add_phone",
                            )
                except Exception as e:
                    logger.warning(f"SMS 接码绑号失败: {e}")

            # 兜底：去掉 prompt=login 再发起一次授权
            if not callback_url:
                no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                if no_prompt_url and no_prompt_url != auth_url:
                    callback_url, final_url = self._follow_authorize_for_callback(
                        no_prompt_url,
                        redirect_uri,
                        "codex_authorize_noprompt",
                    )

            if not callback_url:
                logger.warning("Codex OAuth 未捕获 callback code, final=%s", (final_url or "")[:180])
                return False
            return self._exchange_codex_callback_code(
                callback_url=callback_url,
                expected_state=state,
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        except Exception as e:
            logger.warning(f"Codex OAuth 交换异常: {e}")
            return False

    def _inject_pkce_into_auth_url(self, auth_url: str) -> str:
        """为 authorize URL 注入 PKCE 参数（可选）。"""
        if not auth_url:
            return auth_url
        if not self._env_flag("OAUTH_SECONDARY_PKCE", "0"):
            return auth_url

        try:
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if params.get("code_challenge") and params.get("code_challenge_method"):
                return auth_url

            verifier, challenge = self._build_pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            new_url = urlunparse(parsed._replace(query=urlencode(params)))
            # 若用户未手动指定 verifier，则自动注入本轮 verifier
            if not self._manual_login_verifier:
                self._manual_login_verifier = verifier
            logger.info(
                "已启用二次 PKCE 注入: verifier_len=%s challenge=%s...",
                len(verifier),
                challenge[:16],
            )
            return new_url
        except Exception as e:
            logger.warning(f"注入 PKCE 参数失败，回退原始 auth_url: {e}")
            return auth_url

    @staticmethod
    def _safe_b64url_decode_text(data: str) -> str:
        if not data:
            return ""
        try:
            s = data + "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_hydra_redirect_values(self) -> list[str]:
        """从 hydra_redirect cookie 中提取可能的会话值。"""
        raw = self._get_cookie_value_by_name("hydra_redirect")
        if not raw:
            return []
        out: list[str] = []
        try:
            p0 = (raw.split(".", 1)[0] or "").strip()
            text = self._safe_b64url_decode_text(p0)
            if text:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for v in obj.values():
                        if isinstance(v, str) and v.strip():
                            vv = v.strip()
                            out.append(vv)
                            if "|" in vv:
                                out.extend([x for x in vv.split("|") if isinstance(x, str) and x.strip()])
        except Exception:
            return out
        return out

    def _collect_code_verifier_candidates(self, callback_url: str, continue_url: str) -> list[tuple[str, str]]:
        """收集 code_verifier 候选（来源 + 值）。"""
        raw_candidates: list[tuple[str, str]] = [
            ("query", self._extract_query_first(continue_url, ["login_verifier", "code_verifier", "verifier"])),
            ("query_callback", self._extract_query_first(callback_url, ["login_verifier", "code_verifier", "verifier"])),
            ("dump", self._dump_login_verifier),
            ("captured", self._captured_login_verifier),
            ("manual", self._manual_login_verifier),
            ("cookie_login_verifier", self._get_cookie_value_by_name("login_verifier")),
            ("cookie_code_verifier", self._get_cookie_value_by_name("code_verifier")),
            ("cookie_login_challenge", self._extract_login_challenge_from_cookie()),
            ("cookie_nextauth_state", self._get_cookie_value_by_name("__Secure-next-auth.state")),
        ]

        # hydra_redirect 中可能包含编码后的 csrf/session 串，作为实验候选
        for i, hv in enumerate(self._extract_hydra_redirect_values()):
            raw_candidates.append((f"hydra_{i}", hv))

        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        max_len = max(128, int(os.getenv("OAUTH_MAX_VERIFIER_LEN", "4096")))
        for src, val in raw_candidates:
            v = (val or "").strip()
            if not v:
                continue
            if len(v) > max_len:
                v = v[:max_len]
            if v not in seen:
                seen.add(v)
                out.append((src, v))
            # PKCE 标准长度 43~128；对超长候选补一个截断版本
            if len(v) > 128:
                v128 = v[:128]
                if v128 not in seen:
                    seen.add(v128)
                    out.append((f"{src}_trunc128", v128))

        return out

    def _rotate_impersonate_session(self) -> bool:
        """仅在 curl_cffi 指纹模式内切换 UA 指纹版本重试。"""
        if self._profile_idx >= len(self._browser_profiles) - 1:
            return False
        self._profile_idx += 1
        self._browser_profile = self._browser_profiles[self._profile_idx]
        logger.warning(
            "TLS 异常，切换指纹重试: impersonate=%s ua=%s",
            self._browser_profile.impersonate,
            self._browser_profile.user_agent,
        )
        self.session = create_http_session(proxy=self.config.proxy, profile=self._browser_profile)
        self.result.browser_profile_rotated = True
        self._record_browser_profile()
        return True

    @staticmethod
    def _datadog_trace_headers() -> dict:
        """生成 Datadog APM 追踪头。

        OpenAI 前端集成 Datadog RUM，所有真实浏览器请求都带这 6 个头；
        缺失会被风控判定为非浏览器会话，OTP 邮件等敏感操作会被 silent-drop
        （接口返 200 但邮件不下发）。

        参考 https://github.com/zc-zhangchen/any-auto-register
        platforms/chatgpt/utils.py:generate_datadog_trace（MIT）。
        """
        trace_id = str(random.getrandbits(64))
        parent_id = str(random.getrandbits(64))
        trace_hex = format(int(trace_id), "016x")
        parent_hex = format(int(parent_id), "016x")
        return {
            "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-parent-id": parent_id,
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": trace_id,
        }

    def _common_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """
        构造通用请求头。

        关键点：
        - Origin 必须与 Referer 同源（尤其 auth.openai.com 的状态机接口），
          否则容易触发 invalid_state / 风控分支。
        - 2026-08 手动成功抓包：email-otp/validate 与 create_account **不带**
          oai-device-id 请求头，设备连续性靠 cookie oai-did；因此这里不再主动加。
        - 全请求注入 Datadog trace 头，避免 OTP silent-drop。
        - auth.openai.com 的 JSON 接口补 document-navigation / access-flow 头，
          贴近真浏览器 access 流。
        """
        origin = "https://chatgpt.com"
        try:
            parsed = urlparse(referer or "")
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        headers = {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": origin,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=1, i",
        }
        headers.update(self._browser_profile.headers())

        try:
            host = (urlparse(origin).netloc or "").lower()
        except Exception:
            host = ""
        if "auth.openai.com" in host:
            # 与手动抓包一致：贯穿同一 document navigation / access-flow
            nav_id = str(getattr(self, "_document_navigation_id", "") or "").strip()
            flow_id = str(getattr(self, "_access_flow_invocation_id", "") or "").strip()
            if not nav_id:
                nav_id = str(uuid.uuid4())
                self._document_navigation_id = nav_id
            if not flow_id:
                flow_id = str(uuid.uuid4())
                self._access_flow_invocation_id = flow_id
            headers["x-openai-document-navigation-id"] = nav_id
            headers["x-access-flow-invocation-id"] = flow_id

        headers.update(self._datadog_trace_headers())
        return headers

    def _html_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """构造顶层 HTML/OAuth 导航请求头。"""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-User": "?1",
        }
        headers.update(self._browser_profile.headers())
        return headers

    # ── Step 1: 检查代理连通性 ──
    def check_proxy(self) -> bool:
        """Run the configured network precheck.

        Browser mode uses the browser's own navigation/fetch stack so the
        precheck and the subsequent form flow share the same cookies and
        JavaScript runtime.  Protocol mode keeps the original curl_cffi
        implementation below.
        """

        if self._browser_mode_enabled():
            return bool(self._get_browser_engine().check_proxy())
        return self._check_proxy_protocol()

    def _check_proxy_protocol(self) -> bool:
        logger.info("检查网络连通性...")
        self._proxy_precheck_passed = False
        try:
            resp = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=15)
            trace_ok = False
            if resp.status_code == 200:
                loc = re.search(r"loc=(\w+)", resp.text)
                ip = re.search(r"ip=([^\n]+)", resp.text)
                ip_value = (ip.group(1) if ip else "").strip()
                loc_value = (loc.group(1) if loc else "").strip()
                self.result.outbound_ip = ip_value
                self.result.outbound_ip_version = "ipv6" if ":" in ip_value else ("ipv4" if ip_value else "")
                self.result.outbound_loc = loc_value
                self.result.proxy_trace_status = int(resp.status_code or 0)
                logger.info(f"网络正常 - IP: {ip.group(1) if ip else 'N/A'}, "
                            f"地区: {loc.group(1) if loc else 'N/A'}")
                trace_ok = bool(ip_value)
                if not trace_ok:
                    logger.warning("网络探测异常: cloudflare trace 未返回出口 IP")
            else:
                self.result.proxy_trace_status = int(resp.status_code or 0)
                logger.warning(f"网络探测异常: cloudflare trace {resp.status_code}")

            # 关键链路探测: chatgpt csrf
            csrf_headers = self._common_headers("https://chatgpt.com/auth/login")
            csrf_resp = self.session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers=csrf_headers,
                timeout=20,
            )
            self.result.chatgpt_csrf_status = int(csrf_resp.status_code or 0)
            if csrf_resp.status_code == 200:
                try:
                    csrf_ok = bool((csrf_resp.json() or {}).get("csrfToken"))
                except Exception:
                    csrf_ok = False
                if trace_ok and csrf_ok:
                    self._proxy_precheck_passed = True
                    logger.info("网络预检测通过: cloudflare trace 与 chatgpt csrf 均正常")
                    return True
                if not csrf_ok:
                    logger.warning("chatgpt csrf 响应无有效 csrfToken")
                elif not trace_ok:
                    logger.warning("chatgpt csrf 正常，但 cloudflare 出口 IP 探测未通过")
                return False

            logger.warning(f"chatgpt csrf 连通异常: {csrf_resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"网络检查失败: {e}")
        return False

    # ── Step 2: 获取 CSRF Token ──

    def _ensure_device_id(self) -> str:
        """???????????????? device_id / oai-did?"""
        device_id = (self.result.device_id or "").strip()
        if not device_id:
            try:
                device_id = (self.session.cookies.get("oai-did", "") or "").strip()
            except Exception:
                device_id = ""
        if not device_id:
            device_id = str(uuid.uuid4())
        self.result.device_id = device_id

        # ?????? device_id ????? oai-did cookie?signin query?
        # authorize query ???????? cookie jar??????????
        for domain in (".chatgpt.com", "chatgpt.com", ".openai.com", "auth.openai.com"):
            try:
                self.session.cookies.set("oai-did", device_id, domain=domain)
            except Exception:
                pass
        return device_id

    def open_chatgpt_home(self) -> None:
        """打开 ChatGPT 首页，建立 NextAuth / CF / 完整性 cookie。"""
        logger.info("[0/10] 打开 ChatGPT 首页...")
        headers = self._html_headers("https://chatgpt.com/")
        try:
            resp = self.session.get(
                "https://chatgpt.com/",
                headers=headers,
                timeout=30,
                allow_redirects=True,
            )
            self._trace_http("chatgpt_home", resp)
            logger.info("ChatGPT 首页: %s cookies=%s", getattr(resp, "status_code", "N/A"), self._integrity_cookie_summary())
        except Exception as e:
            logger.warning("打开 ChatGPT 首页失败，继续: %s", e)
        # 纯协议：首页后只做 HTTP cookie 整理，绝不因缺 clearance 拉起浏览器。
        if not self._browser_mode_enabled() or bool(getattr(self, "_force_protocol_mode", False)):
            try:
                self.ensure_integrity_cookies(
                    stage="open_home",
                    required=False,
                )
            except Exception as exc:
                logger.warning("首页 integrity 预热异常: %s", exc)

    def get_auth_providers(self) -> dict:
        """GET /api/auth/providers, matching the captured ChatGPT signup entry flow."""
        logger.info("[1/10] 获取 ChatGPT auth providers...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/providers",
            headers=headers,
            timeout=30,
        )
        self._trace_http("chatgpt_auth_providers", resp)
        if resp.status_code != 200:
            logger.warning("auth providers 返回异常: %s - %s", resp.status_code, (resp.text or "")[:200])
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def get_csrf_token(self) -> str:
        logger.info("[1/10] 获取 CSRF Token...")
        headers = self._common_headers("https://chatgpt.com/auth/login")

        # Cloudflare 可能在短时间内多次请求后返回 403，重试 3 次
        for attempt in range(3):
            try:
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                if self._is_tls_error(e) and self._rotate_impersonate_session():
                    continue
                if self._is_tls_error(e):
                    raise RuntimeError(
                        "chatgpt.com TLS 握手失败，当前网络无法建立到 /api/auth/csrf 的 HTTPS 连接。"
                        "请切换可直连 chatgpt.com 的网络或在界面中配置可用代理后重试。"
                    ) from e
                raise
            if resp.status_code == 403 and attempt < 2:
                wait = (attempt + 1) * 5
                logger.warning(f"Cloudflare 403, {wait}s 后重试 ({attempt + 1}/3)...")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        self._trace_http("chatgpt_csrf", resp)
        csrf = resp.json().get("csrfToken", "")
        if not csrf:
            raise RuntimeError("CSRF Token 获取失败")
        self.result.csrf_token = csrf
        logger.info(f"CSRF Token: {csrf[:20]}...")
        return csrf

    # ── Step 3: 获取 auth URL ──
    def get_auth_url(
        self,
        csrf_token: str,
        email: str = "",
        *,
        screen_hint: str = "login_or_signup",
        prompt: str = "login",
    ) -> str:
        logger.info("[2/10] ?? OpenAI ????...")
        device_id = self._ensure_device_id()
        email = (email or self.result.email or "").strip().lower()
        hint = (screen_hint or "login_or_signup").strip() or "login_or_signup"
        prompt_v = (prompt or "login").strip() or "login"

        query = {
            "prompt": prompt_v,
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": device_id,
            "auth_session_logging_id": self._auth_session_logging_id,
            "screen_hint": hint,
        }
        if email:
            query["login_hint"] = email
        signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)

        headers = self._common_headers("https://chatgpt.com/")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        form = {
            "callbackUrl": "https://chatgpt.com/",
            "csrfToken": csrf_token,
            "json": "true",
        }
        resp = self.session.post(
            signin_url,
            headers=headers,
            data=form,
            timeout=30,
        )
        self._trace_http(
            "chatgpt_signin_openai_captured",
            resp,
            extra_request={"method": "POST", "url": signin_url, "body": urlencode(form), "headers": headers},
        )
        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            raise RuntimeError(f"Auth URL ????: HTTP {resp.status_code} - {body}")
        auth_url = ""
        try:
            auth_url = (resp.json() or {}).get("url", "")
        except Exception:
            auth_url = ""
        if not auth_url:
            auth_url = resp.headers.get("Location", "") or ""
        if not auth_url:
            raise RuntimeError("Auth URL ????: ???? url")
        self._remember_oauth_params(auth_url)
        logger.info(f"Auth URL: {auth_url[:100]}...")
        return auth_url

    # ── Step 4: OAuth 初始化 & 获取 device_id ──
    def auth_oauth_init(self, auth_url: str) -> str:
        logger.info("[3/10] OAuth 初始化...")
        headers = {
            **self._html_headers("https://chatgpt.com/auth/login"),
        }
        try:
            resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)
        except Exception as e:
            if self._is_tls_error(e):
                raise RuntimeError(
                    "OAuth 初始化 TLS/代理握手失败，本轮代理出口异常；请换 sid/IP 后重试。"
                    f" 原始错误: {str(e)[:220]}"
                ) from e
            raise
        self._trace_http("auth_oauth_init", resp)

        # Keep final landing URL for subsequent authorize/continue referer.
        try:
            final_url = str(getattr(resp, "url", "") or auth_url or "").strip()
        except Exception:
            final_url = auth_url or ""
        self._oauth_landed_url = final_url
        if "create-account" in final_url:
            self._oauth_landed_hint = "signup"
        elif "log-in" in final_url or "login" in final_url:
            self._oauth_landed_hint = "login"
        else:
            self._oauth_landed_hint = "login_or_signup"

        # OpenAI rate-limit / soft-block pages often land on /error?payload=...
        # Detect early so callers can rotate proxy instead of hammering continue.
        if "rate_limit_exceeded" in final_url or (
            "/error" in final_url and "payload=" in final_url
        ):
            body_lc = (getattr(resp, "text", "") or "").lower()
            if (
                "rate_limit_exceeded" in final_url
                or "rate limit" in body_lc
                or "too many requests" in body_lc
                or "rate_limit" in body_lc
            ):
                raise RuntimeError(
                    "OAuth 初始化命中 rate_limit_exceeded，当前出口/IP 已被限流；"
                    "请换代理 sid/IP 后重试。"
                    f" landed={final_url[:160]}"
                )

        # 从 cookie 获取 oai-did
        device_id = ""
        for cookie in self.session.cookies:
            if hasattr(cookie, "name"):
                if cookie.name == "oai-did":
                    device_id = cookie.value
                    break
            elif isinstance(cookie, str) and cookie == "oai-did":
                device_id = self.session.cookies.get("oai-did", "")
                break

        # curl_cffi cookies 访问方式
        if not device_id:
            try:
                device_id = self.session.cookies.get("oai-did", "")
            except Exception:
                pass

        # fallback: 从 HTML 提取
        if not device_id:
            m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', resp.text or "")
            if m:
                device_id = m.group(1)

        if not device_id:
            device_id = str(uuid.uuid4())
            logger.warning(f"未从响应中获取 device_id，使用生成值: {device_id}")

        self.result.device_id = device_id
        logger.info(
            "Device ID: %s oauth_landed=%s hint=%s",
            device_id,
            (final_url[:120] if final_url else "(empty)"),
            getattr(self, "_oauth_landed_hint", ""),
        )
        return device_id

    # ── Step 5: 获取 Sentinel Token ──
    def _get_sentinel_token_bundle(self, device_id: str, flow: str) -> dict[str, str]:
        """获取 openai-sentinel-token / openai-sentinel-so-token。

        2026-08 手动抓包确认：email-otp/validate 与 create_account 都同时带了
        token + so-token。so-token 缺失时更容易 registration_disallowed。
        """
        device_id = (device_id or self._ensure_device_id()).strip()
        try:
            from .sentinel_quickjs import get_sentinel_token_bundle_via_quickjs
            bundle = get_sentinel_token_bundle_via_quickjs(
                self.session,
                device_id=device_id,
                flow=flow,
                browser_profile=self._browser_profile.to_dict(),
                log=lambda m: logger.info(m),
            )
            if bundle and bundle.get("token"):
                return {
                    "token": str(bundle.get("token", "") or ""),
                    "so_token": str(bundle.get("so_token", "") or ""),
                }
        except Exception as e:
            logger.warning("Sentinel QuickJS bundle 失败，回退纯 token: %s", e)

        # create_account 强制要求 so-token：禁止回退到纯 Python 假 PoW（易出低信任号）。
        if str(flow or "").strip() == "oauth_create_account":
            logger.warning(
                "create_account Sentinel QuickJS 未产出完整 bundle，拒绝纯 Python 回退"
            )
            return {"token": "", "so_token": ""}

        from .sentinel import get_sentinel_token
        token = get_sentinel_token(
            self.session,
            device_id=device_id,
            flow=flow,
            user_agent=self._browser_profile.user_agent,
        )
        return {"token": token or "", "so_token": ""}

    def get_sentinel_token(self, device_id: str, flow: str = "authorize_continue") -> str:
        logger.info("[4/10] 获取 Sentinel Token (flow=%s)...", flow)
        bundle = self._get_sentinel_token_bundle(device_id, flow)
        token = bundle.get("token", "")
        self._last_sentinel_token = token or ""
        self._last_sentinel_so_token = bundle.get("so_token", "") or ""
        logger.info(
            "Sentinel Token 就绪: token_len=%s so_len=%s flow=%s",
            len(self._last_sentinel_token),
            len(self._last_sentinel_so_token),
            flow,
        )
        return token

    def _apply_sentinel_headers(
        self,
        headers: dict,
        *,
        require_so_token: bool = False,
        step: str = "",
    ) -> dict:
        """把最近一次 Sentinel 产物写入请求头。"""
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if self._last_sentinel_so_token:
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        elif require_so_token:
            raise RuntimeError(
                f"{step or 'request'} 缺少 openai-sentinel-so-token；"
                "真实浏览器抓包中该头为必需，终止本轮避免 registration_disallowed"
            )
        return headers

    def _prepare_sentinel_for_step(
        self,
        flow: str,
        *,
        require_so_token: bool = False,
        attempts: int = 2,
    ) -> dict[str, str]:
        """为指定 flow 刷新 Sentinel；可选强制要求 so-token。"""
        last_bundle = {"token": "", "so_token": ""}
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                last_bundle = self._get_sentinel_token_bundle(self.result.device_id, flow)
            except Exception as e:
                logger.warning("Sentinel flow=%s 第 %s 次失败: %s", flow, attempt, e)
                last_bundle = {"token": "", "so_token": ""}
            token = str(last_bundle.get("token", "") or "").strip()
            so_token = str(last_bundle.get("so_token", "") or "").strip()
            self._last_sentinel_token = token
            self._last_sentinel_so_token = so_token
            if token and (so_token or not require_so_token):
                logger.info(
                    "Sentinel 准备完成: flow=%s attempt=%s token_len=%s so_len=%s",
                    flow,
                    attempt,
                    len(token),
                    len(so_token),
                )
                return {"token": token, "so_token": so_token}
            logger.warning(
                "Sentinel 不完整: flow=%s attempt=%s token_len=%s so_len=%s",
                flow,
                attempt,
                len(token),
                len(so_token),
            )
        if require_so_token and not self._last_sentinel_so_token:
            raise RuntimeError(
                f"Sentinel flow={flow} 未产出 so-token，终止本轮注册"
            )
        if not self._last_sentinel_token:
            raise RuntimeError(f"Sentinel flow={flow} 未产出 token")
        return {
            "token": self._last_sentinel_token,
            "so_token": self._last_sentinel_so_token,
        }

    # ── Step 6: 提交注册邮箱 ──
    def authorize_continue(
        self,
        email: str,
        sentinel_token: str,
        screen_hint: str = "signup",
        referer: str = "https://auth.openai.com/create-account",
        trace_step: str = "",
        *,
        allow_retry: bool = True,
    ) -> dict:
        """调用 /api/accounts/authorize/continue，返回 JSON。"""
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        # Prefer explicit token arg; fall back to latest prepared sentinel bundle.
        token = (sentinel_token or self._last_sentinel_token or "").strip()
        if token:
            headers["openai-sentinel-token"] = token
        if self._last_sentinel_so_token:
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        payload = {
            "username": {"value": email, "kind": "email"},
            "screen_hint": screen_hint,
        }
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json=payload,
            timeout=30,
        )
        self._trace_http(trace_step or f"authorize_continue_{screen_hint}", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:360]
            req_id = (resp.headers.get("x-request-id", "") or "")[:80]
            ct = (resp.headers.get("Content-Type", "") or "")[:60]
            logger.error(
                "authorize/continue 非 200: status=%s screen_hint=%s req_id=%s content_type=%s body=%r",
                resp.status_code, screen_hint, req_id, ct, body,
            )
            body_lc = (body or "").lower()
            if resp.status_code == 429 or "rate_limit_exceeded" in body_lc or "too many requests" in body_lc:
                raise RuntimeError(
                    f"authorize/continue 限流(rate_limit_exceeded): "
                    f"HTTP {resp.status_code} req_id={req_id} body={body}"
                )
            # invalid_state: OAuth session not ready / mismatched. Rebuild once.
            if (
                allow_retry
                and resp.status_code in (400, 409)
                and ("invalid_state" in body_lc or "no longer valid" in body_lc)
            ):
                logger.warning(
                    "authorize/continue invalid_state，重建 OAuth 会话后重试一次 "
                    "(screen_hint=%s)",
                    screen_hint,
                )
                rebuilt = self._rebuild_oauth_session_for_continue(email=email, screen_hint=screen_hint)
                return self.authorize_continue(
                    email=email,
                    sentinel_token=rebuilt.get("sentinel_token", "") or self._last_sentinel_token or "",
                    screen_hint=rebuilt.get("screen_hint", screen_hint) or screen_hint,
                    referer=rebuilt.get("referer", referer) or referer,
                    trace_step=(trace_step or f"authorize_continue_{screen_hint}") + "_retry",
                    allow_retry=False,
                )
            raise RuntimeError(
                f"authorize/continue 失败(screen_hint={screen_hint}): "
                f"HTTP {resp.status_code} req_id={req_id} body={body}"
            )
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def _rebuild_oauth_session_for_continue(
        self,
        *,
        email: str = "",
        screen_hint: str = "signup",
    ) -> dict[str, str]:
        """Rebuild auth.openai.com state after invalid_state on authorize/continue."""

        email = (email or self.result.email or "").strip().lower()
        hint = (screen_hint or "signup").strip().lower() or "signup"
        # Fresh chatgpt auth bootstrap.
        try:
            self.open_chatgpt_home()
        except Exception as e:
            logger.info("rebuild: open_chatgpt_home skip: %s", e)
        try:
            self.get_auth_providers()
        except Exception:
            pass
        csrf = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf, email=email)
        device_id = self.auth_oauth_init(auth_url)
        self.result.device_id = device_id

        landed = str(getattr(self, "_oauth_landed_url", "") or "").strip()
        if "create-account" in landed:
            referer = landed.split("?")[0] or "https://auth.openai.com/create-account"
            hint = "signup"
        elif "log-in" in landed or "login" in landed:
            referer = landed.split("?")[0] or "https://auth.openai.com/log-in"
            if hint == "signup":
                # Force create-account page for passworded registration path.
                try:
                    page = self.session.get(
                        "https://auth.openai.com/create-account",
                        headers=self._html_headers(referer),
                        timeout=20,
                        allow_redirects=True,
                    )
                    self._trace_http("auth_create_account_page_rebuild", page)
                    referer = str(getattr(page, "url", "") or referer).split("?")[0] or referer
                except Exception as e:
                    logger.info("rebuild: open create-account failed: %s", e)
                    referer = "https://auth.openai.com/create-account"
                hint = "signup"
        else:
            # Explicitly open the page matching the desired screen_hint.
            target = (
                "https://auth.openai.com/create-account"
                if hint == "signup"
                else "https://auth.openai.com/log-in"
            )
            try:
                page = self.session.get(
                    target,
                    headers=self._html_headers("https://chatgpt.com/auth/login"),
                    timeout=20,
                    allow_redirects=True,
                )
                self._trace_http("auth_hint_page_rebuild", page)
                referer = str(getattr(page, "url", "") or target).split("?")[0] or target
            except Exception:
                referer = target

        try:
            self._prepare_sentinel_for_step("authorize_continue", require_so_token=False, attempts=2)
        except Exception as e:
            logger.info("rebuild: sentinel prepare skip: %s", e)
        return {
            "screen_hint": hint,
            "referer": referer,
            "sentinel_token": self._last_sentinel_token or "",
        }

    def signup(self, email: str, sentinel_token: str) -> bool:
        """提交注册邮箱。返回 True 表示走新注册流程，False 表示已有账号走 OTP 登录流程"""
        logger.info("[5/10] 提交注册邮箱...")
        # Prefer OAuth landing page as referer; invalid_state often means we POSTed
        # continue against a stale/missing create-account session.
        landed = str(getattr(self, "_oauth_landed_url", "") or "").strip()
        if landed and "auth.openai.com" in landed:
            referer = landed.split("?")[0]
        else:
            referer = "https://auth.openai.com/create-account"
            try:
                page = self.session.get(
                    referer,
                    headers=self._html_headers("https://chatgpt.com/auth/login"),
                    timeout=20,
                    allow_redirects=True,
                )
                self._trace_http("auth_create_account_page_before_signup", page)
                referer = str(getattr(page, "url", "") or referer).split("?")[0] or referer
            except Exception as e:
                logger.info("打开 create-account 页面失败，继续 signup: %s", e)

        data = self.authorize_continue(
            email=email,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer=referer or "https://auth.openai.com/create-account",
            trace_step="authorize_continue_signup",
        )

        # 检测 page_type/continue_url，区分新账号与已有账号
        try:
            page = (data.get("page") or {}) if isinstance(data, dict) else {}
            page_type = (page.get("type") or "").strip()
            payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
            continue_url = (data.get("continue_url") or "").strip()

            # 新账号标准分支
            if page_type == "create_account_password" or "/create-account/password" in continue_url:
                self._is_existing_account = False
                self._existing_email_verification_mode = ""
                self._existing_page_type = page_type
                logger.info("注册邮箱已提交")
                return True

            # 已有账号 OTP 分支 —— 对“密码注册”路径这通常是 OAuth 落地错页导致的假阳性。
            # 允许调用方根据返回值决定是否重建 create-account 会话再试。
            if page_type == "email_otp_verification":
                self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
                self._existing_page_type = page_type
                logger.info(
                    "signup 返回 email_otp_verification（可能邮箱已存在，或 OAuth 未在 create-account 状态）"
                )
                self._is_existing_account = True
                return False

            # 未知 page_type：通常是社交登录/风控分支，按已有账号处理，避免误进 register_password 导致 invalid_state
            self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
            self._existing_page_type = page_type
            self._is_existing_account = True
            logger.warning(
                "authorize/continue 返回非标准注册页面: page_type=%s continue_url=%s，按已有账号流程处理",
                page_type or "(empty)",
                continue_url[:180] or "(empty)",
            )
            return False
        except Exception:
            # JSON 解析失败时保守按新注册处理
            self._is_existing_account = False
            self._existing_email_verification_mode = ""
            self._existing_page_type = ""
            logger.info("注册邮箱已提交")
            return True

    # ── Step 6.5: 注册密码 ──
    def register_password(self, email: str) -> bool:
        logger.info("[5.5/10] 注册密码...")
        # 按需求：密码默认使用注册邮箱，去掉 '@'
        # 例如: abc123@example.com -> abc123example.com
        password = self._default_password_from_email(email)
        self.result.password = password

        # 先访问 create-account/password 页面（HAR 确认需要此步建立服务端状态）
        try:
            pw_page = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._common_headers("https://auth.openai.com/create-account"),
                timeout=15,
            )
            logger.info(f"create-account/password 页面: {pw_page.status_code}")
        except Exception as e:
            logger.warning(f"访问 create-account/password 页面失败: {e}")

        # 注册前刷新 sentinel；优先 dual-token bundle，贴近真实浏览器。
        try:
            self._prepare_sentinel_for_step(
                "username_password_create",
                require_so_token=False,
                attempts=2,
            )
        except Exception as e:
            logger.warning("注册密码前 Sentinel 准备失败: %s", e)

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        self._apply_sentinel_headers(
            headers,
            require_so_token=False,
            step="user/register",
        )
        if not headers.get("openai-sentinel-token"):
            logger.warning("user/register 缺少 openai-sentinel-token")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": email},
            timeout=30,
        )
        self._trace_http("register_password", resp)
        if resp.status_code != 200:
            logger.warning(f"密码注册返回 {resp.status_code}: {resp.text[:200]}")
            return False
        logger.info("密码注册成功 password_len=%s", len(password))
        return True

    # ── Step 7: 发送 OTP ──
    def send_otp(self, referer: str = "https://auth.openai.com/create-account/password"):
        logger.info(f"[6/10] 发送 OTP (referer={referer.split('/')[-1]})...")
        headers = self._common_headers(referer)
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        # zhuce6 用 GET /api/accounts/email-otp/send
        resp = self.session.get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_email_otp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"发送 OTP 失败: {resp.status_code} - {resp.text[:200]}")
        logger.info("OTP 已发送到邮箱")

    def send_passwordless_otp(self, referer: str = "https://auth.openai.com/create-account/password") -> bool:
        """
        走 passwordless 发码（create-account/password 页面可触发该路径）。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_passwordless_otp", resp)
        if resp.status_code == 200:
            logger.info("passwordless OTP 已发送")
            return True
        logger.warning(f"passwordless 发码失败: {resp.status_code} - {(resp.text or '')[:220]}")
        return False

    def resend_otp(self, referer: str = "https://auth.openai.com/email-verification") -> bool:
        """
        重发 OTP（适用于已有账号 passwordless/login_challenge）。
        返回 True 代表请求成功。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        # resend 尽量也带 sentinel，降低 silent-drop / invalid_state
        if not self._last_sentinel_token:
            try:
                self._prepare_sentinel_for_step("authorize_continue", require_so_token=False, attempts=1)
            except Exception:
                pass
        self._apply_sentinel_headers(headers, require_so_token=False, step="email-otp/resend")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("resend_email_otp", resp)
        self._last_resend_otp_status = int(getattr(resp, "status_code", 0) or 0)
        self._last_resend_otp_body = (getattr(resp, "text", "") or "")
        if resp.status_code == 200:
            logger.info("OTP 已重发")
            return True
        logger.warning(f"重发 OTP 失败: {resp.status_code} - {(resp.text or '')[:200]}")
        return False

    def kickoff_otp_delivery(self, mode: str = "") -> bool:
        """
        统一发码策略, 根据 mode hint 区分"新注册" vs "已有账号" referer:

        - 新注册 (create-account/password 页面 state): passwordless/send-otp → email-otp/send
        - 已有账号 / passwordless_login / existing_*: send_otp(referer=email-verification) → resend_otp
          (绕开 passwordless/send-otp 在已有账号场景的 409 invalid_state)
        """
        mode_lc = (mode or "").strip().lower()
        is_existing = (
            "existing" in mode_lc
            or "passwordless_login" in mode_lc
            or "passwordless_signup" in mode_lc  # OpenAI 把 outlook 接码池都打这个 mode
            or self._is_existing_account
        )

        if is_existing:
            # 已有账号 passwordless_signup / passwordless_login: authorize/continue 已经在
            # OpenAI server 端 trigger 了发码 (state S, OTP X, 邮件 X 已在投递). 这里**只能 resend**
            # (复用同 challenge state, 复用同 OTP X 或派生新码但 state 不变). 不能调 send_otp,
            # 它会新建 challenge token 让 state 跳到 Y, 旧邮件 X 在 server 端立即失效 → IMAP 抓到 X
            # verify 时 wrong_email_otp_code.
            if self.resend_otp("https://auth.openai.com/email-verification"):
                return True
            # resend 失败兜底: send_otp 新建 challenge (旧 state 已坏, 不得不重启)
            logger.warning(f"已有账号 resend 失败, 兜底 send_otp 新建 challenge (邮件 X 将失效)")
            try:
                self.send_otp(referer="https://auth.openai.com/email-verification")
                return True
            except Exception as e:
                logger.warning(f"已有账号发码全 fail: {e}")
                return False

        # 新注册 (原顺序)
        if self.send_passwordless_otp("https://auth.openai.com/create-account/password"):
            return True
        if self.resend_otp("https://auth.openai.com/email-verification"):
            return True
        try:
            self.send_otp()
            return True
        except Exception as e:
            logger.warning(f"send_otp 兜底失败(mode={mode_lc or 'unknown'}): {e}")
            return False

    @staticmethod
    def _default_password_from_email(email: str = "") -> str:
        """Generate a random password with letters and digits only.

        No special characters (e.g. * % @ #). Avoids password==email clustering.
        """
        _ = email
        upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        lower = "abcdefghijkmnopqrstuvwxyz"
        digits = "23456789"
        alphabet = upper + lower + digits
        length = random.randint(12, 16)
        chars = [
            random.choice(upper),
            random.choice(lower),
            random.choice(digits),
            random.choice(alphabet),
        ]
        chars.extend(random.choice(alphabet) for _ in range(length - 4))
        random.shuffle(chars)
        return "".join(chars)

    @staticmethod
    def _random_profile_name() -> str:
        """生成 about-you 显示名。

        OpenAI create_account 要求：Name must contain only letters and spaces.
        禁止数字、点号、连字符等。成功抓包样本为纯字母名如 ``alixj``。
        """
        seeds = [
            "alex", "alix", "aria", "ava", "ben", "blake", "cara", "cole",
            "dean", "eden", "eli", "emma", "finn", "grey", "hugo", "iris",
            "ivan", "jade", "jace", "june", "kai", "kyle", "leo", "liam",
            "lina", "luca", "luna", "mark", "maya", "milo", "nate", "nina",
            "noah", "nora", "owen", "paul", "quinn", "reed", "ria", "ryan",
            "sage", "sam", "sara", "sean", "theo", "tina", "vera", "will",
            "zane", "zoe", "mira", "noel", "orin", "remy", "soren", "tess",
        ]
        last_names = [
            "smith", "johnson", "brown", "miller", "davis", "wilson",
            "moore", "taylor", "anderson", "thomas", "jackson", "white",
            "harris", "martin", "thompson", "garcia", "martinez", "robinson",
            "clark", "rodriguez", "lewis", "lee", "walker", "hall", "allen",
            "young", "king", "wright", "scott", "green", "baker", "adams",
            "nelson", "hill", "ramirez", "campbell", "mitchell", "roberts",
            "carter", "phillips", "evans", "turner", "torres", "parker",
            "collins", "edwards", "stewart", "flores", "morris", "nguyen",
        ]
        letters = "abcdefghijklmnopqrstuvwxyz"

        def _letters_only(value: str) -> str:
            cleaned = re.sub(r"[^A-Za-z ]+", "", str(value or ""))
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            return cleaned

        roll = random.random()
        if roll < 0.75:
            # 纯字母单段名（可附加 1~2 个字母尾巴，绝不用数字）
            base = random.choice(seeds)
            if random.random() < 0.55:
                tail_len = random.randint(1, 2)
                base = f"{base}{''.join(random.choice(letters) for _ in range(tail_len))}"
            name = base
        else:
            # First Last，仅字母和空格
            first = random.choice(seeds).capitalize()
            last = random.choice(last_names).capitalize()
            name = f"{first} {last}"

        name = _letters_only(name)
        if not name or not re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+)*", name):
            name = random.choice(seeds)
        return name

    @staticmethod
    def _random_birthdate() -> str:
        """生成合法公历生日。

        成功抓包样本约 20+ 岁（例如 2003-08-05）。
        使用 20~34 的三角分布，众数约 23，避免过老/过小。
        """
        import calendar

        age = int(random.triangular(20, 34, 23))
        year = datetime.now().year - age
        month = random.randint(1, 12)
        day = random.randint(1, calendar.monthrange(year, month)[1])
        return f"{year:04d}-{month:02d}-{day:02d}"

    def login_password_verify(self, password: str) -> dict:
        """已有账号密码登录一步（/password/verify）。"""
        headers = self._common_headers("https://auth.openai.com/log-in/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers,
            json={"password": password},
            timeout=30,
        )
        self._trace_http("login_password_verify", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"密码登录失败: {resp.status_code} - {body}")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Step 8: 验证 OTP ──
    def verify_otp(self, otp_code: str) -> dict:
        """POST /api/accounts/email-otp/validate。

        对齐 GPT模拟注册纯协议：validate 只带 code，不在验码前刷新 Sentinel。
        验码前再跑 authorize_continue sentinel 容易把 auth session 打成 invalid_state(409)。
        需要兼容旧抓包双 token 时设 PROTOCOL_OTP_VALIDATE_SENTINEL=1。
        """
        logger.info("[7/10] 验证 OTP...")
        self.result.last_email_otp_attempt = str(otp_code or "").strip()

        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        # 默认不带 sentinel；历史抓包路径可显式打开
        if self._env_flag("PROTOCOL_OTP_VALIDATE_SENTINEL", "0"):
            try:
                self._prepare_sentinel_for_step(
                    "authorize_continue",
                    require_so_token=False,
                    attempts=1,
                )
            except Exception as e:
                logger.warning("OTP 验证前 Sentinel 准备失败，继续尝试: %s", e)
            self._apply_sentinel_headers(
                headers, require_so_token=False, step="email-otp/validate"
            )

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": str(otp_code or "").strip()},
            timeout=30,
        )
        self._trace_http("validate_email_otp", resp)
        if resp.status_code != 200:
            body = resp.text or ""
            logger.warning("verify_otp FULL body (%s): %s", resp.status_code, body[:2000])
            body_lc = body.lower()
            if resp.status_code == 409 and (
                "invalid_state" in body_lc or "no longer valid" in body_lc
            ):
                raise RuntimeError(
                    "OTP 验证 invalid_state(409)：auth session 已失效，需整单重开注册 "
                    f"(不要 resend 旧会话) body={body[:180]}"
                )
            raise RuntimeError(f"OTP 验证失败: {resp.status_code} - {body[:260]}")
        logger.info("OTP 验证成功")
        self.result.last_successful_email_otp_code = self.result.last_email_otp_attempt
        try:
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _ignore_mail_provider_otp(mail_provider: Any, otp_code: str, reason: str = "") -> None:
        otp_code = str(otp_code or "").strip()
        if not otp_code:
            return
        try:
            if hasattr(mail_provider, "add_ignored_code"):
                mail_provider.add_ignored_code(otp_code)
                logger.info("已将 OTP %s 加入本轮过滤列表%s", otp_code, f" ({reason})" if reason else "")
        except Exception as exc:
            logger.debug("加入 OTP 过滤列表失败: %s", exc)

    @staticmethod
    def _request_next_mail_code(
        mail_provider: Any,
        otp_code: str = "",
        *,
        reason: str = "",
    ) -> None:
        """OTP 验证成功后立刻让邮箱平台进入“接收下一封”(status=5)。

        不要等到整单 registration_succeeded 才点下一封：密码/2FA 邮件可能在
        create_account 之后很快到达，过晚切换会错过。
        """
        otp_code = str(otp_code or "").strip()
        try:
            if otp_code and hasattr(mail_provider, "add_ignored_code"):
                # add_ignored_code 在 SMS Bower 实现里会同时 setStatus=5
                mail_provider.add_ignored_code(otp_code)
                logger.info(
                    "OTP 验证成功，已请求邮箱平台接收下一封%s",
                    f" ({reason})" if reason else "",
                )
                return
            # Fallback: provider may expose request_next_code / set_status helpers.
            for name in ("request_next_code", "prepare_next_code", "wait_next_code"):
                hook = getattr(mail_provider, name, None)
                if callable(hook):
                    hook()
                    logger.info(
                        "OTP 验证成功，已调用 %s 接收下一封%s",
                        name,
                        f" ({reason})" if reason else "",
                    )
                    return
        except Exception as exc:
            logger.warning("请求邮箱平台接收下一封失败: %s", exc)

    # ── Step 9: 创建账户 ──
    def create_account(self) -> str:
        """创建 about-you 资料。

        纯协议默认：直接 POST create_account（PROTOCOL_CREATE_ACCOUNT_BROWSER=0）。
        若显式打开 PROTOCOL_CREATE_ACCOUNT_BROWSER=1，才走浏览器 hybrid 提交。
        """
        logger.info("[8/10] 提交 about_you 创建资料...")
        # create_account 前再确认完整性 cookie（OAuth 过程中可能被冲掉）。
        # 纯协议默认不强制 cf_clearance，避免为了 cookie 再拉起浏览器。
        self.ensure_integrity_cookies(
            stage="pre_create_account",
            required=self._env_flag("PROTOCOL_INTEGRITY_REQUIRED", "0"),
        )

        # 纯协议默认不走浏览器 about-you。
        use_browser = self._env_flag("PROTOCOL_CREATE_ACCOUNT_BROWSER", "0")
        allow_protocol_fallback = self._env_flag(
            "PROTOCOL_CREATE_ACCOUNT_BROWSER_FALLBACK", "1"
        )
        if use_browser and (
            not self._browser_mode_enabled()
            or bool(getattr(self, "_force_protocol_mode", False))
        ):
            try:
                continue_url = self._create_account_via_browser()
                if continue_url:
                    return continue_url
                if not allow_protocol_fallback:
                    raise RuntimeError("浏览器 about-you 未返回 continue_url")
                logger.warning("浏览器 about-you 未返回 continue_url，回退协议 POST")
            except Exception as exc:
                if not allow_protocol_fallback:
                    raise RuntimeError(
                        f"浏览器 about-you 提交失败（已禁用协议回退）: {exc}"
                    ) from exc
                logger.warning(
                    "浏览器 about-you 提交失败，回退协议 POST: %s",
                    exc,
                )

        # 强制要求 so-token：抓包成功路径两者都在；缺失时继续提交易 registration_disallowed
        self._prepare_sentinel_for_step(
            "oauth_create_account",
            require_so_token=True,
            attempts=3,
        )

        headers = self._common_headers("https://auth.openai.com/about-you")
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        # 抓包成功请求带 x-datadog-origin: rum；_common_headers 已有 datadog trace 头。
        headers.setdefault("x-datadog-origin", "rum")
        self._apply_sentinel_headers(
            headers,
            require_so_token=True,
            step="create_account",
        )
        token_len = len(headers.get("openai-sentinel-token") or "")
        so_len = len(headers.get("openai-sentinel-so-token") or "")

        last_error = ""
        data: dict[str, Any] = {}
        # 同会话内最多两次：第一次失败且是 disallowed 时换名/生日再试一次，
        # 避免把明显的环境风控误判成单次坏名字。
        for attempt in range(1, 3):
            name = self._random_profile_name()
            birthdate = self._random_birthdate()
            payload = {"name": name, "birthdate": birthdate}
            logger.info(
                "about_you payload attempt=%s name=%s birthdate=%s token_len=%s so_len=%s ip=%s loc=%s",
                attempt,
                name,
                birthdate,
                token_len,
                so_len,
                getattr(self.result, "outbound_ip", "") or "",
                getattr(self.result, "outbound_loc", "") or "",
            )

            # 第二次尝试时刷新一次 create_account sentinel，降低 token 复用痕迹。
            if attempt > 1:
                try:
                    self._prepare_sentinel_for_step(
                        "oauth_create_account",
                        require_so_token=True,
                        attempts=2,
                    )
                    self._apply_sentinel_headers(
                        headers,
                        require_so_token=True,
                        step="create_account_retry",
                    )
                    token_len = len(headers.get("openai-sentinel-token") or "")
                    so_len = len(headers.get("openai-sentinel-so-token") or "")
                except Exception as exc:
                    logger.warning("create_account 重试刷新 Sentinel 失败: %s", exc)

            resp = self.session.post(
                "https://auth.openai.com/api/accounts/create_account",
                headers=headers,
                json=payload,
                timeout=30,
            )
            self._trace_http(f"create_account_about_you_a{attempt}", resp)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                break

            body = (resp.text or "")[:500]
            last_error = f"{resp.status_code} - {body[:260]}"
            logger.error(
                "创建资料失败 attempt=%s http=%s body=%s ip=%s loc=%s token_len=%s so_len=%s",
                attempt,
                resp.status_code,
                body,
                getattr(self.result, "outbound_ip", "") or "",
                getattr(self.result, "outbound_loc", "") or "",
                token_len,
                so_len,
            )
            if "registration_disallowed" not in body or attempt >= 2:
                if "registration_disallowed" in body:
                    raise RuntimeError(
                        f"创建资料失败: {resp.status_code} - registration_disallowed "
                        f"(邮箱/IP/资料被风控拒绝；建议换 SMS Bower 域名、换代理国家与 sid) "
                        f"ip={getattr(self.result, 'outbound_ip', '') or '-'} "
                        f"loc={getattr(self.result, 'outbound_loc', '') or '-'} "
                        f"body={body[:180]}"
                    )
                raise RuntimeError(f"创建资料失败: {last_error}")
            # disallowed 且还有重试额度：换资料再试
            continue
        else:
            raise RuntimeError(f"创建资料失败: {last_error or 'unknown'}")

        continue_url = self._extract_continue_url_from_step(data)
        self._sniff_login_verifier(continue_url, "create_account_continue_url")

        if not continue_url:
            workspace_id = self._extract_workspace_id()
            if workspace_id:
                continue_url = self._workspace_select(workspace_id)

        if not continue_url:
            continue_url = self._reauthorize_for_session(self._oauth_auth_url) or ""

        if not continue_url:
            raise RuntimeError("创建资料后未获取到 continue_url/callback")

        logger.info("about_you 资料提交成功")
        return continue_url

    def _export_http_cookies_for_browser(self) -> list[dict[str, Any]]:
        """把协议 session cookie 转成 Playwright add_cookies 格式。"""
        cookies: list[dict[str, Any]] = []
        for cookie in self._iter_session_cookies():
            try:
                name = str(getattr(cookie, "name", "") or "").strip()
                value = str(getattr(cookie, "value", "") or "")
                domain = str(getattr(cookie, "domain", "") or "").strip()
                path = str(getattr(cookie, "path", "/") or "/") or "/"
                secure = bool(getattr(cookie, "secure", False))
                expires = getattr(cookie, "expires", None)
            except Exception:
                continue
            if not name or value == "":
                continue
            if not domain:
                # host-only cookie 常见于 __Host- / 部分 CF cookie
                if name.lower().startswith("__host-") or name in {
                    "__Secure-next-auth.session-token",
                    "__Host-next-auth.csrf-token",
                }:
                    domain = "chatgpt.com"
                else:
                    domain = ".chatgpt.com"
            item: dict[str, Any] = {"name": name, "value": value, "path": path}
            if name.lower().startswith("__secure-") or name.lower().startswith("__host-"):
                secure = True
            if name.lower().startswith("__host-"):
                host = domain.lstrip(".") or "chatgpt.com"
                item["url"] = f"https://{host}{path}"
            else:
                item["domain"] = domain
            item["secure"] = secure
            if expires not in (None, "", -1):
                try:
                    item["expires"] = float(expires)
                except (TypeError, ValueError):
                    pass
            cookies.append(item)
        return cookies

    def _import_browser_cookies_to_http(self, cookies: list[dict[str, Any]]) -> int:
        return self._import_cookie_records(list(cookies or []))

    def _create_account_via_browser(self) -> str:
        """用真实浏览器打开 about-you 并提交资料，提升试用资格信任分。

        流程：
        1) 注入协议 OTP 后的 auth cookies
        2) Camoufox/Playwright 打开 about-you，填 name/age|birthday 并提交
        3) 跟随到 chatgpt.com / callback，导回 cookie
        4) 返回 continue_url（若能从 create_account 响应或最终 URL 推断）
        """
        from .browser_session import BrowserSession
        from .config import Config as BrowserConfig
        from .http_client import browser_profile_from_dict

        profile = self._browser_profile
        try:
            profile_dict = profile.to_dict()
        except Exception:
            profile_dict = getattr(self.config, "browser_profile", None) or {}
            if not isinstance(profile_dict, dict):
                profile_dict = {}
            profile = browser_profile_from_dict(profile_dict) or profile
        if not isinstance(profile_dict, dict):
            try:
                profile_dict = profile.to_dict()
            except Exception:
                profile_dict = {}

        engine = (
            str(os.getenv("PROTOCOL_CREATE_ACCOUNT_BROWSER_ENGINE", "") or "").strip().lower()
            or str(os.getenv("PROTOCOL_INTEGRITY_BROWSER_ENGINE", "") or "").strip().lower()
            or "camoufox"
        )
        # 有试用样本是 headed；默认有头藏窗。
        headless = self._env_flag("PROTOCOL_CREATE_ACCOUNT_HEADLESS", "0")
        warm_config = BrowserConfig(
            proxy=getattr(self.config, "proxy", None),
            browser_profile=profile_dict,
            registration_mode="browser",
            browser_engine=engine,
            browser_headless=headless,
            browser_hide_window=True,
            browser_timeout_ms=min(
                90_000,
                max(20_000, int(getattr(self.config, "browser_timeout_ms", 0) or 45_000)),
            ),
        )

        # 降低流量拦截，避免误伤 about-you / sentinel 脚本。
        old_traffic = os.environ.get("BROWSER_TRAFFIC_PROFILE")
        old_block = os.environ.get("BROWSER_BLOCK_RESOURCES")
        os.environ["BROWSER_TRAFFIC_PROFILE"] = os.getenv(
            "PROTOCOL_CREATE_ACCOUNT_TRAFFIC_PROFILE", "normal"
        )
        os.environ["BROWSER_BLOCK_RESOURCES"] = os.getenv(
            "PROTOCOL_CREATE_ACCOUNT_BLOCK_RESOURCES", "0"
        )

        session = BrowserSession(warm_config, profile)
        continue_url = ""
        create_status = 0
        create_body: dict[str, Any] = {}
        page = None
        try:
            session.start()
            # 注入协议侧已有 auth/CF cookie
            seed_cookies = self._export_http_cookies_for_browser()
            if seed_cookies:
                session.add_cookies(seed_cookies)
                logger.info(
                    "browser create_account 注入 cookie=%s",
                    len(seed_cookies),
                )

            page = session.new_page(reuse_existing=True)
            timeout_ms = int(getattr(warm_config, "browser_timeout_ms", 45_000) or 45_000)

            # 监听 create_account 响应（page + context，避免漏事件）
            def _on_response(response: Any) -> None:
                nonlocal create_status, create_body, continue_url
                try:
                    url = str(getattr(response, "url", "") or "")
                    if "create_account" not in url:
                        return
                    create_status = int(getattr(response, "status", 0) or 0)
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                    if isinstance(payload, dict):
                        create_body = payload
                        cont = self._extract_continue_url_from_step(payload)
                        if cont:
                            continue_url = cont
                    logger.info(
                        "browser create_account response status=%s continue=%s url=%s",
                        create_status,
                        bool(continue_url),
                        url[:120],
                    )
                except Exception as exc:
                    logger.debug("create_account response handler err: %s", exc)

            try:
                page.on("response", _on_response)
            except Exception:
                pass
            try:
                ctx = getattr(session, "context", None)
                if ctx is not None:
                    ctx.on("response", _on_response)
            except Exception:
                pass

            # 先打开 about-you（与有试用的浏览器完整 UI 同页提交）
            try:
                page.goto(
                    "https://auth.openai.com/about-you",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            except Exception as exc:
                logger.warning("打开 about-you 被导航打断: %s", exc)

            # 等表单可见
            form_ready = False
            for sel in (
                'input[name="name"]',
                'input[name="full_name"]',
                'input[name="age"]',
                'input[id="age"]',
                'input[type="date"]',
            ):
                try:
                    page.wait_for_selector(sel, state="visible", timeout=12_000)
                    form_ready = True
                    break
                except Exception:
                    continue
            if not form_ready:
                # 可能 cookie 状态已跳过 about-you
                try:
                    url_now = str(getattr(page, "url", "") or "")
                except Exception:
                    url_now = ""
                if "chatgpt.com" in url_now or "/api/auth/callback" in url_now:
                    continue_url = url_now
                    create_status = 200
                else:
                    raise RuntimeError(f"about-you 表单未出现 url={url_now[:160]}")
            try:
                page.wait_for_timeout(900)
            except Exception:
                time.sleep(0.9)

            # 关键：真实 DOM 填表 + 点击提交（对齐有试用浏览器路径）。
            # 历史 browser-context request.post 能 200 但 state=not_eligible。
            prefer_dom = self._env_flag("PROTOCOL_CREATE_ACCOUNT_DOM", "1")
            if prefer_dom and not continue_url:
                from .browser_flow import (
                    NAME_SELECTOR,
                    BIRTHDAY_SELECTOR,
                    _visible,
                    _safe_click_submit,
                    _wait_page,
                )

                name_value = self._random_profile_name()
                birth_iso = self._random_birthdate()
                year_s, month_s, day_s = birth_iso.split("-")
                age_value = str(
                    max(18, min(80, int(time.strftime("%Y")) - int(year_s)))
                )
                logger.info(
                    "browser-dom create_account fill name=%s birthdate=%s age=%s",
                    name_value,
                    birth_iso,
                    age_value,
                )

                name_el = _visible(page, NAME_SELECTOR)
                if name_el is not None:
                    try:
                        name_el.fill(name_value)
                    except Exception:
                        try:
                            name_el.click()
                            page.keyboard.type(name_value, delay=20)
                        except Exception as exc:
                            logger.warning("fill name failed: %s", exc)

                # age 优先（JP 页常见），再兜底 date/birth 字段
                age_el = _visible(page, 'input[name="age"], input[id="age"]')
                if age_el is not None:
                    try:
                        age_el.fill(age_value)
                    except Exception:
                        try:
                            age_el.click()
                            page.keyboard.type(age_value, delay=20)
                        except Exception as exc:
                            logger.warning("fill age failed: %s", exc)
                else:
                    date_el = _visible(page, 'input[type="date"]')
                    if date_el is not None:
                        try:
                            date_el.fill(birth_iso)
                        except Exception as exc:
                            logger.warning("fill date failed: %s", exc)
                    else:
                        birth_el = _visible(page, BIRTHDAY_SELECTOR)
                        if birth_el is not None:
                            try:
                                birth_el.fill(birth_iso)
                            except Exception as exc:
                                logger.warning("fill birthday failed: %s", exc)

                # 勾选同意项（若有）
                try:
                    for checkbox in list(page.query_selector_all('input[type="checkbox"]') or []):
                        try:
                            if not checkbox.is_checked():
                                try:
                                    checkbox.check(force=True)
                                except TypeError:
                                    checkbox.check()
                        except Exception:
                            continue
                except Exception:
                    pass

                clicked = _safe_click_submit(
                    page,
                    pattern=(
                        r"continue|submit|done|next|create|complete|finish|account|"
                        r"続行|続ける|次へ|作成|完了|アカウント|"
                        r"アカウントの作成を完了する"
                    ),
                )
                if not clicked:
                    # 再试一次 requestSubmit / Enter
                    try:
                        page.evaluate(
                            """() => {
                              const form = document.querySelector('form');
                              if (form && typeof form.requestSubmit === 'function') {
                                form.requestSubmit();
                                return true;
                              }
                              const btn = document.querySelector('button[type="submit"], input[type="submit"]');
                              if (btn) { btn.click(); return true; }
                              return false;
                            }"""
                        )
                        clicked = True
                    except Exception as exc:
                        raise RuntimeError(f"about-you 未找到提交按钮: {exc}") from exc
                logger.info("browser-dom about-you submit clicked=%s", clicked)
                _wait_page(page, 800)

                # 等 create_account 响应或离开 about-you
                deadline = time.time() + max(18.0, min(timeout_ms / 1000.0, 45.0))
                while time.time() < deadline:
                    if create_status == 200 and continue_url:
                        break
                    try:
                        url_now = str(getattr(page, "url", "") or "")
                    except Exception:
                        url_now = ""
                    if url_now and (
                        "chatgpt.com" in url_now
                        or "/api/auth/callback" in url_now
                        or "/workspace" in url_now
                        or "/consent" in url_now
                    ):
                        if not continue_url:
                            continue_url = url_now
                        if create_status == 0:
                            create_status = 200
                        break
                    if url_now and "/about-you" not in url_now and "auth.openai.com" in url_now:
                        # 可能已到中间页
                        if not continue_url:
                            continue_url = url_now
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        time.sleep(0.4)

                if create_status not in (0, 200) and create_status != 200:
                    body_preview = ""
                    try:
                        body_preview = json.dumps(create_body, ensure_ascii=False)[:220]
                    except Exception:
                        body_preview = str(create_body)[:220]
                    raise RuntimeError(
                        f"browser-dom create_account HTTP {create_status}: {body_preview}"
                    )
                if create_status == 0 and not continue_url:
                    # 没抓到响应也没跳转：允许一次 protocol-style context POST 兜底（默认关）
                    if not self._env_flag("PROTOCOL_CREATE_ACCOUNT_DOM_API_FALLBACK", "0"):
                        raise RuntimeError(
                            "browser-dom about-you 提交后未观察到 create_account/跳转"
                        )

            # 可选：DOM 失败时 browser-context POST 兜底（默认关闭，避免污染试用判定）
            if (
                not continue_url
                and create_status != 200
                and self._env_flag("PROTOCOL_CREATE_ACCOUNT_DOM_API_FALLBACK", "0")
            ):
                self._prepare_sentinel_for_step(
                    "oauth_create_account",
                    require_so_token=True,
                    attempts=3,
                )
                name = self._random_profile_name()
                birthdate = self._random_birthdate()
                headers = self._common_headers("https://auth.openai.com/about-you")
                headers["Content-Type"] = "application/json"
                headers["Accept"] = "application/json"
                headers.setdefault("x-datadog-origin", "rum")
                self._apply_sentinel_headers(
                    headers,
                    require_so_token=True,
                    step="browser_context_create_account",
                )
                payload = {"name": name, "birthdate": birthdate}
                api = getattr(page, "request", None) or getattr(
                    getattr(session, "context", None), "request", None
                )
                if api is None:
                    raise RuntimeError("browser context request API unavailable")
                try:
                    resp = api.post(
                        "https://auth.openai.com/api/accounts/create_account",
                        headers=headers,
                        data=json.dumps(payload, ensure_ascii=False),
                        timeout=max(20_000, min(timeout_ms, 45_000)),
                    )
                except TypeError:
                    resp = api.post(
                        "https://auth.openai.com/api/accounts/create_account",
                        headers=headers,
                        json=payload,
                        timeout=max(20_000, min(timeout_ms, 45_000)),
                    )
                create_status = int(getattr(resp, "status", 0) or 0)
                try:
                    create_body = resp.json()
                except Exception:
                    create_body = {}
                if isinstance(create_body, dict):
                    cont = self._extract_continue_url_from_step(create_body)
                    if cont:
                        continue_url = cont
                logger.info(
                    "browser-context create_account fallback status=%s continue=%s",
                    create_status,
                    bool(continue_url),
                )
                if create_status != 200:
                    raise RuntimeError(
                        f"browser-context create_account fallback failed: {create_status}"
                    )

            # 跟随 continue_url / 回 chatgpt 落地 session cookie
            try:
                url_now = str(getattr(page, "url", "") or "")
            except Exception:
                url_now = ""
            if continue_url and "auth.openai.com" in continue_url and continue_url != url_now:
                try:
                    page.goto(continue_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    url_now = str(getattr(page, "url", "") or continue_url)
                except Exception as exc:
                    logger.debug("跟随 continue_url 打断: %s", exc)
            # 给 OAuth 跳转一点时间
            settle_deadline = time.time() + 12.0
            while time.time() < settle_deadline:
                try:
                    url_now = str(getattr(page, "url", "") or "")
                except Exception:
                    url_now = ""
                if "chatgpt.com" in (url_now or "") or "/api/auth/callback" in (url_now or ""):
                    if not continue_url:
                        continue_url = url_now
                    break
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    time.sleep(0.4)
            if "chatgpt.com" not in (url_now or ""):
                try:
                    page.goto(
                        "https://chatgpt.com/",
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_timeout(2000)
                    except Exception:
                        time.sleep(2.0)
                    try:
                        page.goto(
                            "https://chatgpt.com/api/auth/session",
                            wait_until="domcontentloaded",
                            timeout=min(timeout_ms, 20_000),
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    logger.debug("browser 回 chatgpt 首页失败: %s", exc)

            # 导回 cookie
            try:
                browser_cookies = session.cookies(
                    urls=[
                        "https://chatgpt.com/",
                        "https://auth.openai.com/",
                        "https://chatgpt.com/api/auth/session",
                    ]
                )
            except Exception:
                browser_cookies = session.cookies()
            imported = self._import_browser_cookies_to_http(list(browser_cookies or []))
            logger.info(
                "browser create_account 完成 status=%s imported_cookies=%s has_cf_clearance=%s continue=%s",
                create_status,
                imported,
                self._has_cf_clearance(),
                (continue_url or "")[:120],
            )

            # 用导回的 cookie 立刻读 session，避免外层只认 continue_url
            try:
                self.get_auth_session()
            except Exception as exc:
                logger.warning("browser create_account 后 get_auth_session 失败: %s", exc)

            if not continue_url:
                try:
                    url_now = str(getattr(page, "url", "") or "")
                except Exception:
                    url_now = ""
                if url_now and (
                    "/api/auth/callback/openai" in url_now or "chatgpt.com" in url_now
                ):
                    continue_url = url_now
                elif self.result.session_token or self.result.access_token:
                    # 已有 session，给一个可跳过 follow 的占位
                    continue_url = "https://chatgpt.com/"
                else:
                    continue_url = self._reauthorize_for_session(self._oauth_auth_url) or ""

            if not continue_url and not (self.result.session_token and self.result.access_token):
                raise RuntimeError("browser create_account 后未获取 continue_url/session")

            if continue_url:
                self._sniff_login_verifier(continue_url, "browser_create_account_continue_url")
            logger.info(
                "browser about_you 资料提交成功 session=%s at=%s",
                bool(self.result.session_token),
                bool(self.result.access_token),
            )
            return continue_url or "https://chatgpt.com/"
        finally:
            try:
                session.close()
            except Exception:
                pass
            # restore traffic env
            if old_traffic is None:
                os.environ.pop("BROWSER_TRAFFIC_PROFILE", None)
            else:
                os.environ["BROWSER_TRAFFIC_PROFILE"] = old_traffic
            if old_block is None:
                os.environ.pop("BROWSER_BLOCK_RESOURCES", None)
            else:
                os.environ["BROWSER_BLOCK_RESOURCES"] = old_block

    def _extract_workspace_id(self) -> str:
        """从 cookie 中提取 workspace_id"""
        try:
            auth_session = self.session.cookies.get("oai-client-auth-session", "")
            if auth_session:
                parts = auth_session.split(".")
                # 兼容不同 cookie 形态：workspace_id 可能在第 1 段/第 2 段，也可能在 workspaces[0].id
                for idx in range(min(2, len(parts))):
                    segment = (parts[idx] or "").strip()
                    if not segment:
                        continue
                    payload_b64 = segment + "=" * (-len(segment) % 4)
                    decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                    if not isinstance(decoded, dict):
                        continue
                    wid = (decoded.get("workspace_id", "") or "").strip()
                    if wid:
                        return wid
                    workspaces = decoded.get("workspaces", [])
                    if isinstance(workspaces, list):
                        for it in workspaces:
                            if isinstance(it, dict):
                                wid = (it.get("id", "") or "").strip()
                                if wid:
                                    return wid
        except Exception:
            pass
        return ""

    def _workspace_select(self, workspace_id: str) -> str:
        logger.info("执行 workspace 选择...")
        headers = self._common_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=30,
        )
        self._trace_http("workspace_select", resp)
        return resp.json().get("continue_url", "") if resp.status_code == 200 else ""

    def _choose_account_select(self, html_text: str, current_url: str) -> str:
        """处理 /choose-an-account 多账号选择页（react-router SSR）。

        HTML 里 streamController.enqueue 注入 `unified_sessions[].id` (us_*) 和
        `session_id` (authsess_*)。这里 regex 抽 us_*，按 react-router action 惯例
        POST 回 /choose-an-account，并 fallback 试几个候选 JSON endpoint。
        返回 next continue_url 或空串。
        """
        m = re.search(r"us_[A-Za-z0-9]{16,}", html_text or "")
        if not m:
            logger.warning("/choose-an-account HTML 里没找到 us_* session id, 跳过")
            return ""
        session_id = m.group(0)
        logger.info(f"/choose-an-account 选 session_id={session_id}")
        headers = self._common_headers("https://auth.openai.com/choose-an-account")
        headers["Origin"] = "https://auth.openai.com"

        # 真实 endpoint 从 nextStepHandler-*.js 反编译解出：
        #   const {path, method} = r.data.intent === "select"
        #     ? {path: "/session/select", method: "POST"}
        #     : {path: "/session/remove", method: "DELETE"};
        #   fetch(`${authapi_base}/session/select`, {method, body: JSON.stringify({session_id})})
        # 即 POST https://auth.openai.com/api/accounts/session/select JSON {session_id}
        # （intent 决定 path 不进 body；body 只有 session_id 一个字段）
        # 之前直接 POST /choose-an-account 会先经过 react-router action loader 再被
        # nextStepHandler 转发，但 server-side 那一段似乎对 CT/form 字段强敏感，500。
        # 直接命中底层 /api/accounts/session/select 绕开 react-router 层。
        candidates = [
            ("POST", "https://auth.openai.com/api/accounts/session/select",
             {"session_id": session_id}, "json"),
            # 兜底：万一上面被风控，回退到 react-router 路径 + zod schema 字段
            ("POST", "https://auth.openai.com/choose-an-account",
             {"intent": "select", "session_id": session_id}, "form"),
        ]
        for method, url, body, kind in candidates:
            try:
                h = dict(headers)
                if kind == "json":
                    h["Content-Type"] = "application/json"
                    h["Accept"] = "application/json"
                    resp = self.session.post(url, headers=h, json=body, timeout=30)
                else:
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                    h["Accept"] = "application/json, text/html;q=0.9"
                    body_str = "&".join(f"{k}={v}" for k, v in body.items())
                    resp = self.session.post(url, headers=h, data=body_str, timeout=30)
                self._trace_http(f"choose_account_try_{kind}_{url.rsplit('/', 1)[-1][:30]}", resp)
                status = getattr(resp, "status_code", 0)
                snippet = (getattr(resp, "text", "") or "")[:240].replace("\n", " ")
                loc = (getattr(resp, "headers", {}) or {}).get("Location", "") or \
                      (getattr(resp, "headers", {}) or {}).get("location", "") or ""
                # print 到 stdout 让 webui SSE 能看到每个候选的具体结果
                print(
                    f"[choose-an-account] {method} {url} [{kind}] -> "
                    f"status={status} loc={loc[:120]} body={snippet}",
                    flush=True,
                )
                if status in (200, 201, 302, 303):
                    next_url = ""
                    try:
                        j = resp.json() if resp is not None else {}
                        next_url = j.get("continue_url", "") if isinstance(j, dict) else ""
                    except Exception:
                        pass
                    if not next_url and loc:
                        next_url = loc
                    if next_url:
                        logger.info(f"choose-an-account 选号成功 endpoint={url} next={next_url[:120]}")
                        return next_url
                    # 200 但没 continue_url：可能 set 了 cookie，直接让 caller 重 GET authorize
                    if status == 200:
                        logger.info(f"choose-an-account POST {url} 200 OK 无 continue_url，假定 cookie 已 set")
                        return current_url  # 让外层重 GET 一次，cookie 已被 server set
            except Exception as e:
                print(f"[choose-an-account] {method} {url} [{kind}] -> EXC {e}", flush=True)
                continue
        logger.warning("/choose-an-account 全部候选 endpoint 都失败")
        return ""

    def _normalize_continue_url(self, continue_url: str) -> str:
        """
        标准化 continue_url：
        1) 相对路径 -> 绝对路径
        2) workspace 页面 -> 调用 workspace/select 取下一跳
        """
        if not continue_url:
            return ""
        out = continue_url.strip()
        if out.startswith("/"):
            out = urljoin("https://auth.openai.com", out)
        if "/workspace" in out:
            workspace_id = self._extract_workspace_id() or self._extract_query_first(out, ["workspace_id", "id"])
            if workspace_id:
                logger.info("检测到 workspace 页面，尝试 workspace/select: workspace_id=%s", workspace_id)
                next_url = self._workspace_select(workspace_id)
                if next_url:
                    out = next_url
        return out

    @staticmethod
    def _extract_workspace_id_from_html(html_text: str) -> str:
        """从 workspace 页面 HTML 文本中提取 workspace_id（兜底）。"""
        if not html_text:
            return ""
        try:
            # 先把转义引号还原，便于正则匹配
            text = html_text.replace('\\"', '"')
            patterns = [
                r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"',
                r'"workspace_id"\s*:\s*"([0-9a-fA-F-]{36})"',
                r'"workspaceId"\s*:\s*"([0-9a-fA-F-]{36})"',
            ]
            for p in patterns:
                m = re.search(p, text, flags=re.DOTALL | re.IGNORECASE)
                if m:
                    return (m.group(1) or "").strip()
        except Exception:
            return ""
        return ""

    # ── Step 10: 跟踪重定向链 ──
    def follow_redirect_chain(self, start_url: str) -> tuple[str, str]:
        """手动跟踪重定向，返回 (callback_url, final_url)"""
        logger.info("[9/10] 跟踪重定向链...")
        current_url = start_url
        callback_url = ""
        max_hops = 12

        for i in range(max_hops):
            headers = {
                **self._html_headers("https://chatgpt.com/"),
            }
            resp = self.session.get(
                current_url, headers=headers, timeout=30, allow_redirects=False
            )
            self._trace_http(f"redirect_hop_{i+1}", resp)

            if "/api/auth/callback/openai" in current_url:
                callback_url = current_url
                self._sniff_login_verifier(current_url, f"redirect_hop_{i+1}_callback_url")

            # workspace 页面常见为 200，需要主动调 workspace/select 获取下一跳
            if "/workspace" in current_url and resp.status_code == 200:
                workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                if workspace_id:
                    logger.info("workspace 页面提取到 workspace_id=%s，尝试继续授权", workspace_id)
                    next_url = self._workspace_select(workspace_id)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current_url = next_url
                        continue

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                # 关键：不要主动 GET callback，避免 code 被服务端回调消费
                if "/api/auth/callback/openai" in location and "code=" in location:
                    callback_url = location
                    current_url = location
                    self._sniff_login_verifier(location, f"redirect_hop_{i+1}_location_callback")
                    logger.info("捕获 callback URL（未消费）")
                    break
                current_url = location
                logger.debug(f"  重定向 {i + 1}: {current_url[:80]}...")
            else:
                break

        # 补一跳首页
        if (not callback_url) and (not current_url.rstrip("/").endswith("chatgpt.com")):
            self.session.get(
                "https://chatgpt.com/",
                headers={"Referer": current_url},
                timeout=30,
            )

        logger.info(f"重定向链完成, callback: {'有' if callback_url else '无'}")
        return callback_url, current_url

    def _reauthorize_for_session(self, original_auth_url: str) -> str | None:
        """已有账号 OTP 验证后，重新发起 authorize 获取 callback URL"""
        logger.info("[9.5/10] 重新 authorize 获取 session ...")
        try:
            # 去掉 prompt=login 参数，利用已有的 auth session cookie
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(original_auth_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("prompt", None)
            # 重新构建 URL
            new_query = urlencode({k: v[0] for k, v in params.items()})
            authorize_url = urlunparse(parsed._replace(query=new_query))

            resp = self.session.get(
                authorize_url,
                allow_redirects=False,
                timeout=15,
            )
            self._trace_http("reauthorize_start", resp)
            logger.info(f"reauthorize status={resp.status_code}")

            # 跟随 redirect chain 找到 callback URL
            current_url = resp.headers.get("Location", "")
            logger.info(f"reauthorize Location: {current_url[:150]}")
            if resp.status_code in (301, 302, 303, 307, 308) and current_url:
                for hop in range(10):
                    logger.debug(f"reauthorize redirect hop {hop+1}: {current_url[:100]}")
                    if "code=" in current_url and "state=" in current_url:
                        logger.info("reauthorize: 找到 callback URL")
                        return current_url
                    try:
                        hop_resp = self.session.get(
                            current_url,
                            allow_redirects=False,
                            timeout=15,
                        )
                        self._trace_http(f"reauthorize_hop_{hop+1}", hop_resp)
                        next_loc = hop_resp.headers.get("Location", "")
                        if hop_resp.status_code not in (301, 302, 303, 307, 308) or not next_loc:
                            # 检查最终 URL
                            final_url = str(getattr(hop_resp, 'url', current_url))
                            if "code=" in final_url:
                                return final_url
                            break
                        current_url = next_loc
                        if not current_url.startswith("http"):
                            from urllib.parse import urljoin
                            current_url = urljoin(authorize_url, current_url)
                    except Exception:
                        break
            logger.warning("reauthorize: 未能获取 callback URL")
            return None
        except Exception as e:
            logger.warning(f"reauthorize 失败: {e}")
            return None

    # ── Step 11: 获取 session ──
    def _extract_session_cookie(self) -> str:
        """多路兜底提取 __Secure-next-auth.session-token cookie。

        curl_cffi 在某些情况下按 domain 隔离 cookie，session.cookies.get(name) 拿不到，
        所以这里把所有 cookie 都遍历一遍，按名字精确匹配。
        """
        target = "__Secure-next-auth.session-token"
        # 路径1：直接 get
        try:
            v = self.session.cookies.get(target, "")
            if v:
                return v
        except Exception:
            pass
        # 路径2：遍历 jar
        try:
            for c in self.session.cookies:
                name = getattr(c, "name", "") if hasattr(c, "name") else str(c)
                if name == target:
                    val = getattr(c, "value", "") or ""
                    if val:
                        return val
        except Exception:
            pass
        # 路径3：用 _get_cookie_value_by_name（不挑 domain）
        try:
            return self._get_cookie_value_by_name(target)
        except Exception:
            return ""

    def get_auth_session(self) -> tuple[str, str]:
        """获取 session_token 和 access_token。

        session_token 三路兜底（按优先级）：
          1. cookie `__Secure-next-auth.session-token`（NextAuth 数据库 session 策略）
          2. JSON 响应里的 `sessionToken` 字段（NextAuth JWT session 策略，某些路径）
          3. 兼容大小写 / 下划线变体
        access_token 取 JSON 响应里的 `accessToken`。
        """
        logger.info("[10/10] 获取认证 Session...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=30,
        )
        self._trace_http("chatgpt_auth_session", resp)
        resp.raise_for_status()

        try:
            parsed_session_json = resp.json() if resp is not None else None
        except Exception:
            parsed_session_json = None
        sess_json = parsed_session_json if isinstance(parsed_session_json, dict) else {}
        self._last_auth_session_json = sess_json
        if isinstance(parsed_session_json, dict):
            response_text = str(getattr(resp, "text", "") or "")
            self.result.auth_session_json = (
                response_text
                if response_text.strip()
                else json.dumps(sess_json, ensure_ascii=False, separators=(",", ":"))
            )

        cookie_st = self._extract_session_cookie()
        json_st = (
            sess_json.get("sessionToken", "")
            or sess_json.get("session_token", "")
            or ""
        )
        session_token = cookie_st or json_st
        access_token = sess_json.get("accessToken", "") or sess_json.get("access_token", "") or ""

        if session_token:
            self.result.session_token = session_token
        if access_token:
            self.result.access_token = access_token
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        logger.info(
            f"session_token: cookie={'有(len=%d)' % len(cookie_st) if cookie_st else '无'} "
            f"json={'有(len=%d)' % len(json_st) if json_st else '无'} "
            f"→ 最终={'有(len=%d)' % len(session_token) if session_token else '无'}; "
            f"access_token={'有(len=%d)' % len(access_token) if access_token else '无'}; "
            f"json_keys={list(sess_json.keys())[:10]}"
        )
        return session_token, access_token

    def _consume_callback_for_session(self, callback_url: str) -> bool:
        """主动 GET callback URL 让 chatgpt.com NextAuth 设 session cookie。

        协议层 follow_redirect_chain 故意不消费 callback（为后续 OAuth token exchange 留 code），
        但这导致 NextAuth 永远不会写 __Secure-next-auth.session-token cookie。
        在拿不到 session_token 时主动消费一次 callback：跟随到 chatgpt.com 主页，
        服务器会 Set-Cookie session-token。
        """
        if not callback_url or "code=" not in callback_url:
            return False
        try:
            current = callback_url
            for hop in range(8):
                resp = self.session.get(
                    current,
                    headers={
                        **self._html_headers("https://auth.openai.com/"),
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"consume_callback_hop_{hop+1}", resp)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = (resp.headers.get("Location", "") or "").strip()
                if not loc:
                    break
                if loc.startswith("/"):
                    loc = urljoin(current, loc)
                current = loc
                # 已到 chatgpt.com 主页就够
                parsed = urlparse(current)
                if "chatgpt.com" in (parsed.netloc or "") and "/api/auth/callback" not in current:
                    # 再 GET 一下主页，让 cookie 全部落地
                    try:
                        self.session.get(current, timeout=20, allow_redirects=True)
                    except Exception:
                        pass
                    break
            return bool(self.session.cookies.get("__Secure-next-auth.session-token", ""))
        except Exception as e:
            logger.warning(f"消费 callback 失败: {e}")
            return False

    # ── 可选: OAuth Token 交换 ──
    def oauth_token_exchange(self, callback_url: str, continue_url: str) -> bool:
        """
        交换 OAuth token（尽力模式）：
        1) 尝试多来源 code_verifier（query/cookie/dump/hydra）
        2) 回退无 verifier
        """
        auth_code = self._extract_query_first(callback_url, ["code"]) or self._extract_query_first(continue_url, ["code"])

        if not auth_code:
            logger.info("缺少 auth_code，跳过 token 交换")
            return False

        verifier_candidates = self._collect_code_verifier_candidates(callback_url, continue_url)
        if not verifier_candidates:
            logger.info("当前未获取到可用 code_verifier，将先尝试无 verifier 交换")
        else:
            show = ", ".join([f"{src}:{len(v)}" for src, v in verifier_candidates[:8]])
            logger.info("code_verifier 候选数=%s 示例=%s", len(verifier_candidates), show)

        logger.info("执行 OAuth Token 交换...")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        base_form = {
            "grant_type": "authorization_code",
            "client_id": self._oauth_client_id or "YOUR_OPENAI_WEB_CLIENT_ID",
            "code": auth_code,
            "redirect_uri": self._oauth_redirect_uri or "https://chatgpt.com/api/auth/callback/openai",
        }
        logger.info(
            "Token 交换参数: client_id=%s redirect_uri=%s",
            base_form["client_id"],
            base_form["redirect_uri"],
        )

        candidates: list[tuple[str, dict]] = []
        if self._oauth_client_secret:
            d = dict(base_form)
            d["client_secret"] = self._oauth_client_secret
            candidates.append(("with_client_secret", d))

        try:
            max_verifier_try = max(1, int(os.getenv("OAUTH_MAX_VERIFIER_TRY", "18")))
        except Exception:
            max_verifier_try = 18

        for src, verifier in verifier_candidates[:max_verifier_try]:
            d = dict(base_form)
            d["code_verifier"] = verifier
            candidates.append((f"with_verifier_{src}", d))
            if self._oauth_client_secret:
                d2 = dict(d)
                d2["client_secret"] = self._oauth_client_secret
                candidates.append((f"with_verifier_{src}_and_client_secret", d2))

        # 一些服务端可能要求额外参数（实验候选）
        audience = self._extract_query_first(self._oauth_auth_url, ["audience"])
        if audience:
            d = dict(base_form)
            d["audience"] = audience
            candidates.append(("without_verifier_with_audience", d))
        if self._oauth_scope:
            d = dict(base_form)
            d["scope"] = self._oauth_scope
            candidates.append(("without_verifier_with_scope", d))

        candidates.append(("without_verifier", dict(base_form)))

        seen_fingerprints: set[str] = set()
        for mode, form in candidates:
            fp = json.dumps(form, sort_keys=True, ensure_ascii=False)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            try:
                self._sniff_login_verifier(urlencode(form), f"oauth_token_exchange_{mode}:form")
            except Exception:
                pass
            encoded_form = urlencode(form)
            extra_request = {
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            }

            resp = self.session.post(
                "https://auth.openai.com/oauth/token",
                headers=headers,
                data=encoded_form,
                timeout=30,
            )
            self._trace_http(f"oauth_token_exchange_{mode}", resp, extra_request=extra_request)
            if resp.status_code == 200:
                data = resp.json()
                self.result.id_token = data.get("id_token", "")
                self.result.access_token = data.get("access_token", self.result.access_token)
                self.result.refresh_token = data.get("refresh_token", "")
                logger.info(
                    "Token 交换成功(mode=%s): refresh_token=%s",
                    mode,
                    "有" if self.result.refresh_token else "无",
                )
                return True

            body = (resp.text or "")[:240]
            logger.warning("Token 交换失败(mode=%s): status=%s body=%s", mode, resp.status_code, body)

        return False

    def oauth_secondary_authorize_exchange(self) -> bool:
        """
        二次授权实验：
        - 在当前已登录会话上，重新发起一条带 PKCE 的 authorize
        - 仅提取 callback code，不消费 callback
        - 再走 oauth/token 交换
        """
        logger.info("尝试二次 authorize + PKCE 换 refresh_token ...")
        try:
            csrf = self.get_csrf_token()
            auth_url = self.get_auth_url(csrf)
        except Exception as e:
            logger.warning(f"二次 authorize 初始化失败: {e}")
            return False

        try:
            verifier, challenge = self._build_pkce_pair()
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            if not params.get("state"):
                params["state"] = self._b64url_no_pad(os.urandom(16))
            sec_url = urlunparse(parsed._replace(query=urlencode(params)))

            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            self._remember_oauth_params(sec_url)

            current = sec_url
            callback_url = ""
            max_hops = 10
            for i in range(max_hops):
                resp = self.session.get(
                    current,
                    headers={
                        **self._html_headers("https://chatgpt.com/"),
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"secondary_authorize_hop_{i+1}", resp)

                loc = (resp.headers.get("Location", "") or "").strip()
                if loc and loc.startswith("/"):
                    loc = urljoin(current, loc)

                if loc and "/api/auth/callback/openai" in loc and "code=" in loc:
                    callback_url = loc
                    break
                if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                    break
                current = loc

            if not callback_url:
                logger.warning("二次 authorize 未捕获 callback code")
                return False

            ok = self.oauth_token_exchange(callback_url, callback_url)
            logger.info("二次 authorize 交换结果: %s", "成功" if ok else "失败")
            return ok
        except Exception as e:
            logger.warning(f"二次 authorize 交换异常: {e}")
            return False

    # ── 完整注册流程 ──
    def run_register(self, mail_provider: MailProvider) -> AuthResult:
        """Run either the browser-backed or the historical protocol flow."""

        if self._browser_mode_enabled():
            try:
                return self._get_browser_engine().run_register(mail_provider)
            except Exception:
                # Browser mode is intentionally explicit.  A fallback can be
                # enabled for staged rollouts, but is off by default so a
                # failed browser attempt never silently consumes a second
                # mailbox or changes the requested registration semantics.
                if not bool(getattr(self.config, "browser_fallback_to_protocol", False)):
                    raise
                logger.exception("浏览器注册失败，按配置回退协议链路")
                try:
                    if self._browser_engine is not None:
                        self._browser_engine.close()
                except Exception:
                    pass
                reuse_email = (self.result.email or "").strip()
                fallback_provider = (
                    _FixedMailboxProvider(mail_provider, reuse_email)
                    if reuse_email
                    else mail_provider
                )
                self._force_protocol_mode = True
                try:
                    return self._run_register_protocol(fallback_provider)
                finally:
                    self._force_protocol_mode = False
        return self._run_register_protocol(mail_provider)

    def _run_register_protocol(self, mail_provider: MailProvider) -> AuthResult:
        """纯协议注册（从手动 CDP 抓包从零实现）。

        抓包样本: outputs/captures/manual_signup_full_20260816_060202_20260816_060202
        人工路径:
          主页注册 → 输入邮箱 → (可先到验证码页) → 切换密码注册
          → 设密码提交 → 邮箱验证码 → 姓名/生日 → 回调主页

        协议映射（全程 HTTP，不启动浏览器）:
          1) chatgpt.com/  + /api/auth/providers + /api/auth/csrf
          2) POST /api/auth/signin/openai?prompt=login&screen_hint=signup&login_hint=EMAIL&ext-oai-did=DID
          3) GET  auth.openai.com/api/accounts/authorize?...&screen_hint=signup&login_hint=EMAIL
          4) GET  /create-account/password
          5) sentinel flow=username_password_create
             POST /api/accounts/user/register  {"password","username"}
          6) GET  continue_url=/api/accounts/email-otp/send  (document)
          7) sentinel flow=email_otp_validate  (token + so-token)
             POST /api/accounts/email-otp/validate {"code"}
          8) GET  /about-you
          9) sentinel flow=oauth_create_account (token + so-token)
             POST /api/accounts/create_account {"name","birthdate"}
         10) GET  chatgpt.com/api/auth/callback/openai?code=...
         11) GET  chatgpt.com/api/auth/session
         12) TOTP 2FA: mfa_info → mfa/enroll → activate_enrollment（best-effort）
        """
        if not self._proxy_precheck_passed and not self.check_proxy():
            raise RuntimeError("网络预检测失败，已终止本轮注册，未领取邮箱")

        # 强制纯协议
        os.environ["PROTOCOL_CREATE_ACCOUNT_BROWSER"] = "0"
        os.environ["PROTOCOL_INTEGRITY_BROWSER_WARM"] = "0"
        os.environ.setdefault("PROTOCOL_INTEGRITY_REQUIRED", "0")
        os.environ.setdefault("PROTOCOL_INTEGRITY_REQUIRE_CF_CLEARANCE", "0")
        os.environ.setdefault("REGISTRATION_MODE", "protocol")
        self._force_protocol_mode = True

        # 领邮箱
        email = (mail_provider.create_mailbox() or "").strip().lower()
        if not email:
            raise RuntimeError("邮箱 Provider 未返回邮箱")
        self.result.email = email
        password = self._default_password_from_email(email)
        self.result.password = password

        # 1) chatgpt 会话种子
        self._ensure_device_id()
        self.open_chatgpt_home()
        self.get_auth_providers()
        csrf = self.get_csrf_token()
        device_id = self._ensure_device_id()
        self.result.device_id = device_id

        # 2) NextAuth signin/openai（抓包带 screen_hint=signup + login_hint）
        auth_url = self.get_auth_url(csrf_token=csrf, email=email, screen_hint="signup")
        if "login_hint=" not in auth_url and email:
            sep = "&" if "?" in auth_url else "?"
            auth_url = f"{auth_url}{sep}login_hint={email}"
        if "screen_hint=" not in auth_url:
            sep = "&" if "?" in auth_url else "?"
            auth_url = f"{auth_url}{sep}screen_hint=signup"

        # 3) 打开 authorize，建立 auth.openai.com 会话
        device_id = self.auth_oauth_init(auth_url)
        self.result.device_id = device_id
        logger.info("capture-protocol OAuth ready device_id=%s", device_id)

        # 4) 进入密码注册页
        try:
            pw_page = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._html_headers("https://auth.openai.com/email-verification"),
                timeout=20,
                allow_redirects=True,
            )
            self._trace_http("auth_create_account_password_page", pw_page)
            logger.info(
                "create-account/password page status=%s",
                getattr(pw_page, "status_code", "?"),
            )
        except Exception as exc:
            logger.warning("打开 create-account/password 失败，继续 register: %s", exc)

        # 5) user/register（sentinel flow=username_password_create）
        if not self.register_password(email):
            raise RuntimeError("user/register 失败")

        # 6) 抓包：register 返回 continue_url=email-otp/send，用 GET 文档导航触发发码
        try:
            otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "90")))
        except Exception:
            otp_timeout = 90

        otp_sent_at = time.time()
        send_headers = self._html_headers("https://auth.openai.com/create-account/password")
        try:
            send_resp = self.session.get(
                "https://auth.openai.com/api/accounts/email-otp/send",
                headers=send_headers,
                timeout=30,
                allow_redirects=True,
            )
            self._trace_http("email_otp_send_document", send_resp)
            logger.info(
                "email-otp/send status=%s final_url=%s",
                getattr(send_resp, "status_code", "?"),
                str(getattr(send_resp, "url", "") or "")[:120],
            )
        except Exception as exc:
            logger.warning("document GET email-otp/send 失败，回退 API send_otp: %s", exc)
            self.send_otp("https://auth.openai.com/create-account/password")
            otp_sent_at = time.time()

        try:
            ver_page = self.session.get(
                "https://auth.openai.com/email-verification",
                headers=self._html_headers("https://auth.openai.com/create-account/password"),
                timeout=20,
                allow_redirects=True,
            )
            self._trace_http("auth_email_verification_page", ver_page)
        except Exception as exc:
            logger.warning("打开 email-verification 失败: %s", exc)

        try:
            otp_code = mail_provider.wait_for_otp(
                email, timeout=otp_timeout, issued_after=otp_sent_at
            )
        except TimeoutError:
            logger.warning("OTP 超时，再 GET email-otp/send 一次后重试")
            otp_sent_at = time.time()
            try:
                self.session.get(
                    "https://auth.openai.com/api/accounts/email-otp/send",
                    headers=self._html_headers("https://auth.openai.com/email-verification"),
                    timeout=30,
                    allow_redirects=True,
                )
            except Exception:
                self.send_otp("https://auth.openai.com/email-verification")
            otp_code = mail_provider.wait_for_otp(
                email, timeout=otp_timeout, issued_after=otp_sent_at
            )

        # 7) validate：抓包 flow=email_otp_validate，双 token
        otp_resp = self._verify_otp_capture(otp_code)
        self._request_next_mail_code(mail_provider, otp_code, reason="register otp verified")
        self.fetch_client_auth_session_dump("post_verify_otp_captured_register")

        page_type = self._extract_page_type(otp_resp).lower()
        continue_url = self._normalize_continue_url(
            self._extract_continue_url_from_step(otp_resp)
        )
        logger.info("otp validate page=%s continue=%s", page_type or "-", bool(continue_url))

        # 8) about-you + create_account
        if page_type == "about_you" or "/about-you" in (continue_url or "") or not continue_url:
            try:
                about = self.session.get(
                    "https://auth.openai.com/about-you",
                    headers=self._html_headers("https://auth.openai.com/email-verification"),
                    timeout=20,
                    allow_redirects=True,
                )
                self._trace_http("auth_about_you_page", about)
            except Exception as exc:
                logger.warning("打开 about-you 失败: %s", exc)
            continue_url = self._normalize_continue_url(self.create_account())

        if not continue_url:
            raise RuntimeError("create_account 后未拿到 continue_url/callback")

        # 9-11) callback → session
        callback_url = ""
        if "/api/auth/callback/openai" in continue_url and "code=" in continue_url:
            callback_url = continue_url
        else:
            callback_url, _final = self.follow_redirect_chain(continue_url)

        if callback_url:
            logger.info("消费 OAuth callback ...")
            self._consume_callback_for_session(callback_url)

        self.get_auth_session()
        if callback_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_register")
            if (
                self._env_flag("OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK", "0")
                and not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0")
            ):
                self.oauth_token_exchange(callback_url, continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            self.get_auth_session()

        if not self.result.is_valid():
            raise RuntimeError("注册完成但未获取有效 session/access_token")

        if self._env_flag("PROTOCOL_BOOTSTRAP_TRIAL_PROBE", "1"):
            try:
                self.bootstrap_chatgpt_client_and_probe_trial()
            except Exception as exc:
                logger.warning("试用探测失败（不阻断）: %s", exc)

        # 12) 设置 2FA（失败不阻断注册结果）
        if self._env_flag("PROTOCOL_ENABLE_TOTP", "1"):
            try:
                self._enable_totp_after_protocol_register()
            except Exception as exc:
                logger.warning("协议注册后设置 2FA 异常（不阻断）: %s", exc)
                if not self.result.totp_status:
                    self.result.totp_status = "failed"
                    self.result.totp_error = f"{type(exc).__name__}: {exc}"[:500]
                    self.result.mfa_enabled = False

        logger.info(
            "抓包协议注册完成 email=%s totp=%s",
            email,
            self.result.totp_status or "n/a",
        )
        return self.result

    def _enable_totp_after_protocol_register(self) -> None:
        """注册成功后 best-effort 开通 TOTP 2FA。

        链路（mfa_totp_protocol）:
          GET  /backend-api/accounts/mfa_info
          POST /backend-api/accounts/mfa/enroll
          本地算 TOTP
          POST /backend-api/accounts/mfa/user/activate_enrollment
        失败只写 totp_status/totp_error，不丢账号。
        """
        access_token = str(self.result.access_token or "").strip()
        if not access_token:
            self.result.totp_status = "failed"
            self.result.totp_error = "missing_access_token"
            self.result.mfa_enabled = False
            logger.warning("协议 TOTP 跳过：缺少 access_token email=%s", self.result.email[:120])
            return

        cookie_header = str(self.result.cookie_header or "").strip()
        session_token = str(self.result.session_token or "").strip()
        if not cookie_header and session_token and not session_token.startswith("{"):
            cookie_header = f"__Secure-next-auth.session-token={session_token}"

        from .mfa_totp_protocol import enable_totp_for_token

        email = str(self.result.email or "chatgpt").strip()
        proxy = str(getattr(self.config, "proxy", "") or "").strip()
        logger.info("协议注册后开通 TOTP email=%s", email[:120])
        try:
            raw = enable_totp_for_token(
                access_token,
                cookie_header=cookie_header,
                device_id=str(self.result.device_id or "").strip(),
                proxy=proxy,
                account_name=email,
                max_enroll_attempts=2,
                settle_seconds=1.0,
                window=1,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.result.totp_status = "failed"
            self.result.totp_error = f"transport: {detail}"[:500]
            self.result.mfa_enabled = False
            logger.warning("协议 TOTP transport 失败 email=%s err=%s", email[:120], detail[:300])
            return

        if not isinstance(raw, dict):
            self.result.totp_status = "failed"
            self.result.totp_error = "non_mapping_response"
            self.result.mfa_enabled = False
            return

        if not bool(raw.get("ok")) or not bool(raw.get("mfa_enabled")):
            detail = str(raw.get("error") or "mfa_not_enabled")[:300]
            self.result.totp_status = "failed"
            self.result.totp_error = detail
            self.result.mfa_enabled = False
            partial_secret = str(raw.get("secret") or "").strip()
            if partial_secret:
                self.result.two_factor = {
                    "enabled": False,
                    "factor_type": str(raw.get("factor_type") or "totp").strip() or "totp",
                    "secret": partial_secret,
                    "otpauth_uri": str(raw.get("otpauth_uri") or "").strip(),
                    "factor_id": str(raw.get("factor_id") or "").strip(),
                    "status": "enroll_unconfirmed",
                }
            logger.warning(
                "协议 TOTP 未确认 email=%s err=%s（账号仍保留）",
                email[:120],
                detail,
            )
            return

        secret = str(raw.get("secret") or "").strip()
        already = str(raw.get("error") or "") == "already_enabled"
        if not secret and not already:
            self.result.totp_status = "failed"
            self.result.totp_error = "missing_persistable_secret"
            self.result.mfa_enabled = bool(raw.get("mfa_enabled"))
            logger.warning("协议 TOTP 缺 secret email=%s（账号仍保留）", email[:120])
            return

        two_factor = {
            "enabled": True,
            "factor_type": str(raw.get("factor_type") or "totp").strip() or "totp",
            "secret": secret,
            "otpauth_uri": str(raw.get("otpauth_uri") or "").strip(),
            "factor_id": str(raw.get("factor_id") or "").strip(),
            "status": "already_enabled" if already else "enabled",
        }
        self.result.two_factor = two_factor
        self.result.mfa_enabled = True
        self.result.totp_status = "already_enabled" if already else "enabled"
        self.result.totp_error = ""
        logger.info(
            "协议 TOTP 开通成功 email=%s factor_id=%s status=%s",
            email[:120],
            two_factor["factor_id"][:16],
            self.result.totp_status,
        )

    def _verify_otp_capture(self, otp_code: str) -> dict:
        """按抓包实现 email-otp/validate。

        抓包要求:
          - referer: https://auth.openai.com/email-verification
          - json: {"code": "..."}
          - openai-sentinel-token + openai-sentinel-so-token
          - flow = email_otp_validate
        """
        logger.info("[7/10] 验证 OTP (capture flow=email_otp_validate)...")
        code = str(otp_code or "").strip()
        self.result.last_email_otp_attempt = code
        if not code:
            raise RuntimeError("OTP 为空")

        try:
            self._prepare_sentinel_for_step(
                "email_otp_validate",
                require_so_token=True,
                attempts=3,
            )
        except Exception as exc:
            logger.warning(
                "email_otp_validate sentinel 准备失败，尝试 authorize_continue: %s",
                exc,
            )
            try:
                self._prepare_sentinel_for_step(
                    "authorize_continue",
                    require_so_token=True,
                    attempts=2,
                )
            except Exception as exc2:
                logger.warning("OTP sentinel 回退也失败: %s", exc2)

        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        self._apply_sentinel_headers(
            headers, require_so_token=True, step="email-otp/validate"
        )
        if not headers.get("openai-sentinel-token"):
            logger.warning("email-otp/validate 缺少 openai-sentinel-token")
        if not headers.get("openai-sentinel-so-token"):
            logger.warning("email-otp/validate 缺少 openai-sentinel-so-token")

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": code},
            timeout=30,
        )
        self._trace_http("validate_email_otp_capture", resp)
        if resp.status_code != 200:
            body = resp.text or ""
            logger.warning(
                "verify_otp FULL body (%s): %s", resp.status_code, body[:2000]
            )
            body_lc = body.lower()
            if resp.status_code == 409 and (
                "invalid_state" in body_lc or "no longer valid" in body_lc
            ):
                raise RuntimeError(
                    "OTP 验证 invalid_state(409)：auth session 已失效，需整单重开 "
                    f"body={body[:180]}"
                )
            raise RuntimeError(f"OTP 验证失败: {resp.status_code} - {body[:260]}")

        logger.info("OTP 验证成功")
        self.result.last_successful_email_otp_code = code
        try:
            return resp.json()
        except Exception:
            return {}

    def _profile_language_primary(self) -> str:
        """主语言：JP 抓包用 ja-JP。"""
        lang = str(getattr(self._browser_profile, "language", "") or "").strip()
        if lang:
            return lang
        accept = str(getattr(self._browser_profile, "accept_language", "") or "").strip()
        if accept:
            return accept.split(",")[0].split(";")[0].strip() or "en-US"
        return "en-US"

    def _timezone_offset_min(self) -> int:
        """从 profile.timezone 推 timezone_offset_min；JP 默认 -540。"""
        tz_name = str(getattr(self._browser_profile, "timezone", "") or "").strip()
        if tz_name in {"Asia/Tokyo", "Japan"}:
            return -540
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            if tz_name:
                offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
                if offset is not None:
                    return -int(offset.total_seconds() // 60)
        except Exception:
            pass
        return -540

    def _chatgpt_client_headers(
        self,
        *,
        referer: str = "https://chatgpt.com/",
        include_auth: bool = True,
    ) -> dict[str, str]:
        """构造接近真浏览器 accounts/check / check_coupon 的请求头。"""
        headers = self._common_headers(referer)
        headers["Accept"] = "*/*"
        primary_lang = self._profile_language_primary()
        headers["Accept-Language"] = str(
            getattr(self._browser_profile, "accept_language", "") or primary_lang
        )
        device_id = self._ensure_device_id()
        headers["oai-device-id"] = device_id
        headers["oai-language"] = primary_lang
        headers["oai-session-id"] = str(
            getattr(self, "_chatgpt_session_id", "") or uuid.uuid4()
        )
        # 版本号尽量贴近近期前端；缺失时用稳定占位，避免空头。
        headers.setdefault(
            "oai-client-version",
            str(os.getenv("OAI_CLIENT_VERSION", "prod-protocol-bootstrap") or "prod-protocol-bootstrap"),
        )
        headers.setdefault(
            "oai-client-build-number",
            str(os.getenv("OAI_CLIENT_BUILD_NUMBER", "9114028") or "9114028"),
        )
        if include_auth and (self.result.access_token or "").strip():
            headers["Authorization"] = f"Bearer {self.result.access_token.strip()}"
        return headers

    def bootstrap_chatgpt_client_and_probe_trial(
        self,
        *,
        campaign: str = "plus-1-month-free",
    ) -> dict[str, Any]:
        """注册同出口：主页预热 → accounts/check → promo referer → check_coupon。

        目的：补齐浏览器回主站后的客户端信任链，并把试用探测结果写到 result.trial_probe。
        不抛致命错误；探测失败时返回 status=error/ineligible 供上层决定是否再换 JP sid。
        """
        probe: dict[str, Any] = {
            "status": "error",
            "campaign_id": campaign,
            "detail": "",
            "source": "protocol_bootstrap",
            "checked_at": time.time(),
            "proxy_region": str(getattr(self.result, "outbound_loc", "") or ""),
            "amount_minor": None,
            "amount_currency": "JPY",
            "billing_country": "JP",
            "attempts": 1,
        }
        if not (self.result.access_token or self.result.session_token):
            probe["detail"] = "bootstrap skipped: missing credentials"
            self.result.trial_probe = probe
            return probe

        # 刷新 cookie_header，尽量带上注册会话 jar（oai-sc 等）。
        try:
            self.result.cookie_header = self._build_chatgpt_cookie_header()
        except Exception:
            pass

        try:
            home_headers = self._html_headers("https://chatgpt.com/")
            home = self.session.get(
                "https://chatgpt.com/",
                headers=home_headers,
                timeout=30,
                allow_redirects=True,
            )
            self._trace_http("chatgpt_home_post_register", home)
        except Exception as exc:
            logger.warning("注册后打开 chatgpt 主页失败: %s", exc)

        try:
            self.get_auth_session()
        except Exception as exc:
            logger.warning("注册后刷新 auth session 失败: %s", exc)

        tz_offset = self._timezone_offset_min()
        try:
            check_headers = self._chatgpt_client_headers(
                referer="https://chatgpt.com/",
                include_auth=True,
            )
            check_resp = self.session.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                headers=check_headers,
                params={"timezone_offset_min": str(tz_offset)},
                timeout=30,
            )
            self._trace_http("accounts_check_post_register", check_resp)
            if int(getattr(check_resp, "status_code", 0) or 0) == 200:
                try:
                    check_payload = check_resp.json()
                except Exception:
                    check_payload = None
                if isinstance(check_payload, dict):
                    # 若 accounts/check 已带 plus-1-month-free，直接记 eligible。
                    blob = json.dumps(check_payload, ensure_ascii=False)
                    if campaign in blob and "eligible_promo_campaigns" in blob:
                        probe["status"] = "eligible"
                        probe["detail"] = "accounts/check eligible_promo_campaigns"
                        probe["source"] = "protocol_bootstrap/accounts_check"
                        probe["amount_minor"] = 0
        except Exception as exc:
            logger.warning("注册后 accounts/check 失败: %s", exc)

        promo_referer = f"https://chatgpt.com/?promo_campaign={campaign}"
        try:
            promo_headers = self._html_headers("https://chatgpt.com/")
            promo_page = self.session.get(
                promo_referer,
                headers=promo_headers,
                timeout=30,
                allow_redirects=True,
            )
            self._trace_http("chatgpt_promo_landing", promo_page)
        except Exception as exc:
            logger.warning("打开 promo 落地页失败: %s", exc)

        try:
            coupon_headers = self._chatgpt_client_headers(
                referer=promo_referer,
                include_auth=True,
            )
            coupon_url = (
                "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
                f"?coupon={campaign}&is_coupon_from_query_param=true"
            )
            coupon_resp = self.session.get(
                coupon_url,
                headers=coupon_headers,
                timeout=30,
            )
            self._trace_http("check_coupon_post_register", coupon_resp)
            status_code = int(getattr(coupon_resp, "status_code", 0) or 0)
            if status_code == 200:
                try:
                    payload = coupon_resp.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    state = str(payload.get("state") or "").strip().lower()
                    redemption = (
                        payload.get("redemption")
                        if isinstance(payload.get("redemption"), dict)
                        else {}
                    )
                    redeemed = bool(
                        redemption.get("redeemed") or redemption.get("redeemed_by_user")
                    )
                    if state == "eligible" and not redeemed:
                        probe["status"] = "eligible"
                        probe["detail"] = "check_coupon:state=eligible"
                        probe["source"] = "protocol_bootstrap/check_coupon"
                        probe["amount_minor"] = 0
                    elif state in {
                        "ineligible",
                        "not_eligible",
                        "invalid",
                        "expired",
                        "redeemed",
                    } or redeemed:
                        probe["status"] = "ineligible"
                        probe["detail"] = f"check_coupon:state={state or 'redeemed'}"
                        probe["source"] = "protocol_bootstrap/check_coupon"
                    else:
                        probe["detail"] = f"check_coupon:state={state or 'unknown'}"
                        probe["source"] = "protocol_bootstrap/check_coupon"
                else:
                    probe["detail"] = "check_coupon non-json"
            elif status_code == 401:
                probe["detail"] = "check_coupon HTTP 401"
            else:
                # 403 等保持 error，便于入库后再换 JP sid 复测，避免误标 ineligible。
                probe["detail"] = f"check_coupon HTTP {status_code}"
        except Exception as exc:
            probe["detail"] = f"check_coupon error: {type(exc).__name__}: {exc}"
            logger.warning("注册后 check_coupon 失败: %s", exc)

        try:
            self.result.cookie_header = self._build_chatgpt_cookie_header()
        except Exception:
            pass
        self.result.trial_probe = probe
        logger.info(
            "注册后试用预热完成 status=%s detail=%s loc=%s lang=%s",
            probe.get("status"),
            str(probe.get("detail") or "")[:160],
            getattr(self.result, "outbound_loc", "") or "",
            self._profile_language_primary(),
        )
        return probe

    def run_protocol_login(self, mail_provider: MailProvider, email: str, password: str = "") -> AuthResult:
        """Run browser login when enabled, otherwise protocol login."""

        if self._browser_mode_enabled():
            try:
                return self._get_browser_engine().run_login(mail_provider, email, password)
            except Exception:
                if not bool(getattr(self.config, "browser_fallback_to_protocol", False)):
                    raise
                logger.exception("浏览器登录失败，按配置回退协议链路")
                try:
                    if self._browser_engine is not None:
                        self._browser_engine.close()
                except Exception:
                    pass
                self._force_protocol_mode = True
                try:
                    return self._run_protocol_login(mail_provider, email, password)
                finally:
                    self._force_protocol_mode = False
        return self._run_protocol_login(mail_provider, email, password)

    def _run_protocol_login(self, mail_provider: MailProvider, email: str, password: str = "") -> AuthResult:
        """
        纯协议登录（不创建随机邮箱）：
        - 适配 passwordless / login_password 两类已有账号入口
        - 可配合 OAUTH_EXCHANGE_BEFORE_CALLBACK / OAUTH_REFRESH_ONLY 尝试优先拿 refresh_token
        """
        if not (email or "").strip():
            raise RuntimeError("run_protocol_login 缺少邮箱")

        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试登录链路以获取精确错误...")

        # run_protocol_login 的语义即"登录已有账号"（docstring 明写）。kickoff_otp_delivery
        # 依据 _is_existing_account 选 resend vs send_passwordless_otp 分支；落到 send
        # 分支会把 server-side state 弄坏 → 之后 IMAP 抓到的 OTP X 已失效 → verify 401
        # wrong_email_otp_code。这里入口统一 set True，覆盖 passwordless 这类 page_type
        # 不在 ("login_password","email_otp_verification") 集合的情况；signup() 回退
        # 路径会基于 OpenAI 真实响应再次覆盖（True/False），无副作用。
        self._is_existing_account = True

        email = email.strip()
        self.result.email = email
        login_password = (password or "").strip() or self._default_password_from_email(email)
        self.result.password = login_password

        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, email=email)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)

        continue_url = ""
        try:
            otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "60")))
        except Exception:
            otp_timeout = 180

        page_type = ""
        mode = ""
        prefer_login_screen_first = str(
            os.getenv("LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT", "1")
        ).lower() in ("1", "true", "yes", "on")

        if prefer_login_screen_first:
            try:
                logger.info("已有账号协议登录：优先走 login screen_hint 探测 password/otp 分支")
                login_step = self.authorize_continue(
                    email=email,
                    sentinel_token=sentinel,
                    screen_hint="login",
                    referer="https://auth.openai.com/log-in",
                    trace_step="authorize_continue_login_protocol",
                )
                page_type = (self._extract_page_type(login_step) or "").lower()
                continue_url = self._normalize_continue_url(
                    self._extract_continue_url_from_step(login_step)
                )
                page = (login_step.get("page") or {}) if isinstance(login_step, dict) else {}
                payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
                mode = (payload.get("email_verification_mode", "") or "").lower()
                self._existing_page_type = page_type
                self._existing_email_verification_mode = mode

                if page_type == "login_password" or "/log-in/password" in (continue_url or ""):
                    logger.info("登录分支: login_password -> password/verify")
                    # 命中已有账号 password 路径：标记之，让 kickoff_otp_delivery 走 resend
                    # 分支（避免 send_passwordless_otp 把 state 弄坏 → wrong_email_otp_code）
                    self._is_existing_account = True
                    login_resp = self.login_password_verify(login_password)
                    page_type = (self._extract_page_type(login_resp) or "").lower()
                    continue_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(login_resp)
                    )
                elif page_type == "email_otp_verification" or "/email-verification" in (continue_url or ""):
                    logger.info("登录分支: email_otp_verification")
                    # 同上：authorize/continue 已 trigger 发码，kickoff_otp_delivery 必须只 resend。
                    self._is_existing_account = True
                else:
                    logger.info(
                        "login screen_hint 未直接命中已有账号完成态: page_type=%s continue_url=%s",
                        page_type or "(empty)",
                        (continue_url or "")[:180] or "(empty)",
                    )
            except Exception as e:
                logger.warning(f"login screen_hint 探测失败，回退 signup 探测: {e}")
                continue_url = ""
                page_type = ""
                mode = ""

        if not continue_url and page_type not in ("login_password", "email_otp_verification"):
            is_new = self.signup(email, sentinel)
            if is_new:
                logger.warning("目标邮箱未命中已有账号分支，回退到注册链路")
                self.register_password(email)
                otp_sent_at = time.time()
                self.send_otp()
                otp_code = mail_provider.wait_for_otp(
                    email,
                    timeout=otp_timeout,
                    issued_after=otp_sent_at,
                )
                self.verify_otp(otp_code)
                continue_url = self.create_account()
            else:
                page_type = (self._existing_page_type or "").lower()
                mode = (self._existing_email_verification_mode or "").lower()
        else:
            page_type = (page_type or self._existing_page_type or "").lower()
            mode = (mode or self._existing_email_verification_mode or "").lower()

        if not continue_url or "/email-verification" in continue_url:
            # 仍需 OTP：优先 resend 获取新码
            otp_sent_at = time.time()
            resend_ok = self.kickoff_otp_delivery("protocol_need_otp")
            if not resend_ok and mode not in ("passwordless_signup", "passwordless_login"):
                self.send_otp()
                otp_sent_at = time.time()

            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                otp_resp = self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_protocol")
            except RuntimeError as e:
                if any(code in str(e) for code in ("401", "409")):
                    logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                    self._ignore_mail_provider_otp(mail_provider, otp_code, "verify failed before retry")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("protocol_verify_retry"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_protocol")
                else:
                    raise
            continue_url = self._extract_continue_url_from_step(otp_resp)
            continue_url = self._normalize_continue_url(continue_url)
            if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                continue_url = self._normalize_continue_url(
                    self._handle_add_phone_verification(continue_url=continue_url)
                )

        continue_url = self._normalize_continue_url(continue_url)
        # 某些边缘态 OTP 后未返回 callback，回退 reauthorize
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        skip_oauth_token_exchange = self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0")
        callback_url = ""
        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            if (
                (not self.result.refresh_token)
                and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1")
                and not skip_oauth_token_exchange
            ):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange and not skip_oauth_token_exchange:
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if not refresh_only_mode:
            self.get_auth_session()

        if (callback_url or continue_url) and not skip_oauth_token_exchange:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_protocol")
            self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (
                (not self.result.refresh_token)
                and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1")
            ):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (
                (not self.result.refresh_token)
                and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0")
            ):
                self.oauth_secondary_authorize_exchange()
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("协议登录完成，但未拿到 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("协议登录完成，但未拿到有效 session/access token")

        logger.info("纯协议登录流程完成")
        return self.result

    # ── 从已有凭证初始化 ──
    def from_existing_credentials(
        self, session_token: str, access_token: str, device_id: str
    ) -> AuthResult:
        """使用已有凭证（跳过注册）"""
        self.result.device_id = device_id or str(uuid.uuid4())
        self.session.cookies.set("oai-did", self.result.device_id, domain=".chatgpt.com")
        detected_email = ""

        # 如果有 session_token, 用它刷新 access_token (旧 access_token 可能已过期)
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
            logger.info("使用 session_token 刷新 access_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                new_access_token = session_data.get("accessToken", "")
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                new_session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if new_access_token:
                    access_token = new_access_token
                    logger.info("access_token 刷新成功")
                else:
                    logger.warning(f"access_token 刷新失败 (status={resp.status_code}), 使用原 token")
                if new_session_token:
                    session_token = new_session_token
            except Exception as e:
                logger.warning(f"刷新 access_token 失败: {e}, 使用原 token")
        elif access_token:
            # 没有 session_token, 尝试通过 access_token 获取
            logger.info("未提供 session_token, 尝试通过 access_token 获取...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                headers["Authorization"] = f"Bearer {access_token}"
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if session_token:
                    logger.info("通过 access_token 获取 session_token 成功")
                else:
                    logger.warning("未能获取 session_token, 可能需要手动提供")
            except Exception as e:
                logger.warning(f"获取 session_token 失败: {e}")

        self.result.access_token = access_token
        self.result.session_token = session_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        # 回填 email（skip-register 模式下常用于账单 email）
        if not detected_email and access_token and access_token.count(".") >= 2:
            try:
                payload_b64 = access_token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                prof = payload.get("https://api.openai.com/profile", {}) if isinstance(payload, dict) else {}
                if isinstance(prof, dict):
                    detected_email = detected_email or (prof.get("email", "") or "")
            except Exception:
                pass
        self.result.email = detected_email or ""
        logger.info("使用已有凭证初始化完成")
        return self.result
