# -*- coding: utf-8 -*-
"""
ChatGPT 协议注册全流程入口
串联 12 个步骤，自动完成 ChatGPT 账号注册
"""
import sys
import argparse
import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

from config import REGISTER_EMAIL, REGISTER_NAME  # 这两个一般不在 WebUI 改
# 可热改的，按模块属性方式读
from config import twofa as _twofa_cfg
from config import email as _email_cfg
from config import roxybrowser as _roxy_cfg
from config import openai_protocol as _protocol_cfg
from config import register as _register_cfg
from core.session import BrowserSession, close_browser_session
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    request_sentinel_token,
    build_sentinel_header,
    validate_email_otp,
    network_preflight,
    navigate_about_you,
    navigate_create_account_password,
    navigate_email_otp_send,
    request_password_sentinel_bundle,
    generate_registration_password,
    register_user,
    EmailOtpInvalidError,
    create_account,
)
from core.account_export import (
    fetch_session,
    setup_2fa,
    save_account_data,
    create_batch_archive_dir,
)
from core.email_provider import acquire_email, wait_for_otp
from core.humanize import delay as human_delay
from core.name_samples import random_display_name
from core.profile_utils import generate_random_birthday
from core.protocol_registration import (
    resolve_signup_step, continue_existing_login, needs_email_otp,
    resend_registration_otp, select_registration_workspace,
    follow_registration_callback, step_info, landing_step, recover_registration_disallowed,
    install_registration_checkpoint,
    checkpoint_registration_password,
    reauthorize_registration_session,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_FINALIZE_SESSION_MAX_ATTEMPTS = 5
_FINALIZE_SESSION_BACKOFF_BASE = 2.0


def configure_logging(verbose: bool = False) -> None:
    """配置 CLI 日志：默认简洁，--verbose 时显示完整步骤细节。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in root.handlers:
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    if verbose:
        logging.getLogger("core").setLevel(logging.DEBUG)
        return

    logging.getLogger("core").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _is_success(result: dict) -> bool:
    """判断单次注册结果是否成功，集中收敛批量统计规则。"""
    return isinstance(result, dict) and bool(result.get("success"))


def _protocol_registration_fingerprint_seed(email: str) -> str | None:
    """按配置决定协议注册是否跨任务复用同邮箱指纹。"""
    if not bool(getattr(_register_cfg, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", False)):
        return None
    identity = str(email or "").strip().lower()
    return f"registration:{identity}" if identity else None


def _finalize_registration_session(
    session: BrowserSession,
    continue_url: str,
    email: str,
    callback_referer: str = "https://auth.openai.com/about-you",
) -> tuple[dict, str]:
    """
    完成 OAuth 回调并拉取 accessToken。

    create_account 返回只代表创建接口通过，真正可用必须等 chatgpt.com
    写入登录态 cookie 且 /api/auth/session 返回 accessToken。
    """
    if not continue_url:
        raise RuntimeError("create_account 响应缺少 continue_url，无法完成 OAuth 回调")

    from core.registration_service import StopRequested
    from core.openai_auth import (
        _interruptible_sleep, _is_retryable_authorize_error, _reset_retryable_circuit,
    )

    last_exc: Exception | None = None
    callback_completed = False
    for attempt in range(1, _FINALIZE_SESSION_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"[登录态] 完成 OAuth 回调并拉取 Token：{email} "
                f"(尝试 {attempt}/{_FINALIZE_SESSION_MAX_ATTEMPTS})"
            )
            if not callback_completed:
                follow_registration_callback(session, continue_url, referer=callback_referer)
                callback_completed = True
                human_delay("post_auth")
            session_info = fetch_session(session)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("session 响应缺少 accessToken")
            user = session_info.get("user")
            session_email = user.get("email") if isinstance(user, dict) else None
            if session_email and str(session_email).strip().casefold() != str(email).strip().casefold():
                raise RuntimeError("session 邮箱与注册目标不匹配，停止保存账号")
            logger.info(f"[登录态] 已拿到 accessToken：{email}")
            return session_info, access_token
        except StopRequested:
            raise
        except Exception as exc:
            last_exc = exc
            session_pending = callback_completed and any(message in str(exc) for message in (
                "session 响应缺少 accessToken", "未拿到 accessToken，登录态可能未建立",
            ))
            if not session_pending and not _is_retryable_authorize_error(exc):
                raise
            if getattr(session, "_registration_callback_started", False) is True:
                callback_completed = True
            if attempt >= _FINALIZE_SESSION_MAX_ATTEMPTS:
                break
            _reset_retryable_circuit(session)
            backoff = _FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[登录态] 回调或拉取 Token 失败：{email}，"
                f"{type(exc).__name__}: {str(exc)[:180]}，{backoff:.1f}s 后重试"
            )
            # code 已消费时只重试 session，不能再次重放同一个 OAuth callback。
            _interruptible_sleep(backoff)

    raise RuntimeError(
        f"OAuth 回调/拉取 Token 重试耗尽：{email}，"
        f"最后错误：{type(last_exc).__name__ if last_exc else 'Unknown'}: {last_exc}"
    ) from last_exc


def generate_display_name() -> str:
    """生成只包含英文字母和空格的显示名，符合注册接口限制。"""
    return random_display_name()


def prepare_registration_inputs() -> tuple[str | None, str, str]:
    """按 CLI 规则准备一次注册所需的邮箱、显示名和生日。"""
    email = REGISTER_EMAIL
    name = REGISTER_NAME
    birthday = generate_random_birthday()

    # 邮箱：自动模式下先留空；浏览器驱动会在页面找到邮箱输入框后领取，
    # protocol 驱动会在 run_registration 开始认证前领取。
    if not email:
        if not _email_cfg.USE_EMAIL_SERVICE:
            email = input("请输入注册邮箱: ").strip()

    # 显示名称：未填则随机生成
    # OpenAI 限制：name_invalid_chars —— 只允许字母和空格，不能含数字/标点
    if not name:
        if _email_cfg.USE_EMAIL_SERVICE:
            name = generate_display_name()
            logger.debug(f"自动生成显示名称: {name}")
        else:
            name = input("请输入显示名称: ").strip()

    if not name:
        raise RuntimeError("显示名称不能为空")
    if not email and not _email_cfg.USE_EMAIL_SERVICE:
        raise RuntimeError("邮箱不能为空")

    return email, name, birthday


def run_registration(
    email: str | None,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
    on_email_acquired: Callable[[str], None] | None = None,
):
    """
    执行 ChatGPT 密码注册；已有账号按服务端状态恢复密码/OTP/MFA 登录。

    OpenAI 当前默认流程：signin 时携带 login_hint+screen_hint=login_or_signup
    → 进入 /create-account/password 并提交密码 → 发送并验证邮箱 OTP
    → about-you 提交昵称生日 → 完成。

    Args:
        email: 注册邮箱
        name: 用户显示名称
        birthday: 生日，格式 YYYY-MM-DD
        proxy: 代理地址（不传则从 PROXY_POOL 随机抽）
        otp_code: 邮箱验证码（如果为None，会等待手动输入）
    """
    # 可选注册驱动：
    #   protocol     = 原有纯协议（curl_cffi）
    #   roxy         = RoxyBrowser 指纹浏览器 + Selenium
    #   cloak        = CloakBrowser + Playwright/Selenium 适配层
    #   browser_use  = Browser Use Cloud stealth Chromium + Playwright
    #   skyvern      = Skyvern Browser Sessions + Playwright
    driver_mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
    if driver_mode in ("roxy", "roxybrowser", "fingerprint", "browser"):
        from core.roxy_registration import run_roxy_registration
        return run_roxy_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            on_email_acquired=on_email_acquired,
        )
    if driver_mode in ("cloak", "cloakbrowser"):
        from core.cloakbrowser_registration import run_cloak_registration
        return run_cloak_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            on_email_acquired=on_email_acquired,
        )
    if driver_mode in ("browser_use", "browseruse", "browser-use", "bu"):
        from core.browser_use_registration import run_browser_use_registration
        return run_browser_use_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            on_email_acquired=on_email_acquired,
        )
    if driver_mode in ("skyvern", "sv"):
        from core.skyvern_registration import run_skyvern_registration
        return run_skyvern_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            on_email_acquired=on_email_acquired,
        )
    if driver_mode not in ("protocol", "api", "http"):
        raise RuntimeError(
            f"不支持的 REGISTRATION_DRIVER={driver_mode!r}，可选 protocol / roxy / cloak / browser_use / skyvern"
        )

    from core.registration_service import StopRequested, check_stop_requested
    session = None
    create_acknowledged = False
    credentials_saved = False
    password_saved = False
    registration_password = None
    try:
        # 纯协议驱动没有“邮箱输入框”可等待，因此在创建 BrowserSession 前领取。
        if not str(email or "").strip():
            if not _email_cfg.USE_EMAIL_SERVICE:
                raise RuntimeError(
                    "手动模式未配置邮箱。请在 WebUI 配置页设置 REGISTER_EMAIL，"
                    "或开启 USE_EMAIL_SERVICE 并从邮箱池领取。"
                )
            email = acquire_email()
            if on_email_acquired:
                on_email_acquired(email)

        # 创建浏览器会话（proxy=None 时自动从 config.PROXY_POOL 随机抽一个）
        fingerprint_seed = _protocol_registration_fingerprint_seed(email)
        reuse_fingerprint = fingerprint_seed is not None
        # 单个 BrowserSession 生命周期内始终复用同一指纹；是否跨任务按邮箱
        # 稳定复用由 PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL 控制。
        session = BrowserSession(
            proxy=proxy,
            fingerprint_seed=fingerprint_seed,
        )

        # 从代理 URL 中抽取 sid 段做日志，避免把账号密码完整打印
        proxy_label = "无"
        if session.proxy:
            # 形如 socks5h://user-region-JP-sid-XXXX-t-5:pass@host:port
            try:
                sid_part = next(
                    (seg for seg in session.proxy.split("@")[0].split("-") if len(seg) == 8),
                    "***",
                )
                proxy_label = f"{session.proxy.split('://')[0]}://...sid-{sid_part}...@{session.proxy.split('@')[-1]}"
            except Exception:
                proxy_label = "已配置"

        if not birthday:
            birthday = generate_random_birthday()

        logger.info(f"[注册] 开始：{email}，代理={proxy_label}")
        logger.info(
            "[指纹] 生命周期模式：%s",
            "同邮箱保持" if reuse_fingerprint else "每次任务重新创建",
        )
        logger.info(f"[注册] 本次随机生日: {birthday}")
        fp = session.fingerprint_summary()
        logger.info(
            "[指纹] 协议注册统一上下文: device_id=%s oai_session_id=%s auth_session_logging_id=%s "
            "ua=%s lang=%s tz=%s(%s) screen=%sx%s@%s cpu=%s mem=%s geo=%s:%s",
            session.device_id[:12] + "...",
            session.oai_session_id[:12] + "...",
            session.auth_session_logging_id[:12] + "...",
            BrowserSession._short_value(fp.get("user_agent"), 72),
            fp.get("accept_language"),
            fp.get("timezone_iana"),
            fp.get("timezone_offset_minutes"),
            fp.get("screen_width"), fp.get("screen_height"), fp.get("device_pixel_ratio"),
            fp.get("hardware_concurrency"), fp.get("device_memory"),
            fp.get("geo_country") or "?", fp.get("geo_city") or "?",
        )

        # 网络预检必须在 signin/follow_authorize 之前完成；预检不带邮箱，不会触发 OTP。
        network_preflight(session)
        human_delay("navigate")

        # 根据 2026-07-19 HAR 补齐匿名态 ChatGPT 首屏/模型预热链路。
        if getattr(_protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", True):
            from core.chatgpt_bootstrap import anonymous_bootstrap
            anonymous_bootstrap(
                session,
                strict=bool(getattr(_protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
            )
            human_delay("navigate")

        # ==================== 阶段1: ChatGPT 认证 ====================
        # 步骤1: 获取 providers
        providers = get_providers(session)
        human_delay("api")

        # 步骤2: 获取 CSRF token
        csrf_token = get_csrf_token(session)
        human_delay("api")

        # 步骤3: 发起 OAuth signin
        authorize_url = signin_openai(session, csrf_token, email)
        human_delay("api")

        # ==================== 阶段2: OpenAI Auth ====================
        # 步骤4: 跟随 authorize URL（建立 auth.openai.com 的 cookies）
        authorize_started_at = time.time()
        authorize_final_url = follow_authorize(session, authorize_url)
        human_delay("navigate")

        # 按服务端落点分流；已登录/已有账号不重复 user/register。
        signup_kind, initial_step = resolve_signup_step(session, email, authorize_final_url)
        validate_result = None
        otp_after_ts = authorize_started_at
        if signup_kind == "new":
            navigate_create_account_password(session, authorize_final_url)
            human_delay("navigate")
            registration_password = generate_registration_password()
            password_sentinel = request_password_sentinel_bundle(session)
            password_sentinel_header, password_so_header = build_sentinel_header(
                session, password_sentinel, "username_password_create"
            )
            human_delay("challenge")
            register_result = register_user(
                session, email, registration_password,
                password_sentinel_header, password_so_header,
            )
            create_acknowledged = True
            checkpoint_registration_password(session, email, registration_password)
            password_saved = True
            otp_after_ts = time.time()
            _, send_url = step_info(register_result)
            navigate_email_otp_send(session, send_url or None)
            human_delay("navigate")
        elif signup_kind in ("about_you", "external_url", "add_phone"):
            create_acknowledged = True
            validate_result = dict(initial_step)
            # 先规范化 URL，再改 page.type，避免丢掉 page.payload.url。
            _, initial_url = step_info(initial_step)
            if initial_url:
                validate_result["continue_url"] = initial_url
            validate_result["page"] = {"type": signup_kind}
        else:
            create_acknowledged = True  # 已有账号不能因恢复失败放回新注册邮箱池。
            login_started_at = time.time()
            registration_password, login_step = continue_existing_login(
                session, email, signup_kind, initial_step,
            )
            if needs_email_otp(login_step):
                page, _ = step_info(login_step)
                if page == "passwordless_login":
                    pass  # authorize 已发码，直接使用本轮时间下界，不额外改变认证状态。
                elif login_step.get("_otp_already_sent") is True:
                    otp_after_ts = login_started_at
                else:
                    otp_after_ts = time.time()
                    resend_registration_otp(session, existing=True)
            else:
                validate_result = login_step

        # ==================== 阶段3: 验证码验证 ====================
        # Sentinel Token 不提前生成；等 OTP 到手后紧贴 validate 请求生成，
        # 避免等待邮箱期间 challenge 过期或与重新发送后的状态不一致。

        # 等待验证码：USE_EMAIL_SERVICE=True 时自动从 Outlook 取件，否则人工输入。
        # 如果验证码错误/过期，自动重新发送并重新取最新验证码。
        max_otp_attempts = 3
        current_otp = otp_code
        for otp_attempt in range(1, max_otp_attempts + 1):
            if validate_result is not None:
                break
            if current_otp is None:
                from core import registration_service as registration_svc
                web_manual = getattr(registration_svc._THREAD_CTX, "job_id", None) is not None
                if _email_cfg.USE_EMAIL_SERVICE or web_manual:
                    logger.info(f"[OTP] 等待验证码：{email}（第 {otp_attempt}/{max_otp_attempts} 次）")
                    try:
                        current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                    except TimeoutError:
                        if otp_attempt >= max_otp_attempts:
                            raise
                        logger.warning("[OTP] 等待超时，在当前认证状态内重发后继续等待")
                        otp_after_ts = time.time()
                        resend_registration_otp(session, existing=signup_kind != "new")
                        continue
                else:
                    logger.info("")
                    logger.info(f"[OTP] 请检查邮箱，输入收到的 6 位验证码（第 {otp_attempt}/{max_otp_attempts} 次）:")
                    current_otp = input(">>> 验证码: ").strip()

            human_delay("otp_input")
            try:
                # 验证 OTP 必须使用本次 email_otp_validate flow，而不是 authorize_continue。
                sentinel_header_9 = None
                so_header_9 = None
                if getattr(_protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", False):
                    sentinel_resp_9 = request_sentinel_token(session, "email_otp_validate")
                    sentinel_header_9, so_header_9 = build_sentinel_header(session, sentinel_resp_9, "email_otp_validate")
                    human_delay("challenge")

                # 步骤10: 提交验证码
                validate_result = validate_email_otp(session, current_otp, sentinel_header_9, so_header_9)
                break
            except EmailOtpInvalidError as exc:
                if otp_attempt >= max_otp_attempts:
                    raise
                logger.warning(f"[OTP] 验证码错误/过期：{str(exc)[:180]}，准备重新发送并重新获取验证码")
                otp_after_ts = time.time()
                resend_registration_otp(session, existing=signup_kind != "new")
                human_delay("api")
                current_otp = None

        if validate_result is None:
            raise RuntimeError("OTP 验证未完成")
        human_delay("api")

        # OTP 校验后的下一步由服务端 auth session 决定：
        #   - about_you：新账号，需要继续提交姓名/生日 create_account。
        #   - external_url：通常说明服务端已可直接 OAuth 回调（常见于已有账号/无需资料页），
        #                   此时再强行调用 create_account 会触发 invalid_auth_step。
        page_type, otp_continue_url = step_info(validate_result)
        if signup_kind != "new" and not page_type and not otp_continue_url:
            otp_continue_url = reauthorize_registration_session(session, email)
            page_type = "external_url"
        # 按服务端状态推进条件挑战，不能只处理一次 MFA 然后忽略 add-phone。
        seen_challenges = set()
        for _ in range(4):
            challenge_kind = landing_step(otp_continue_url) if otp_continue_url else page_type
            if challenge_kind not in ("mfa_challenge", "add_phone"):
                challenge_kind = page_type
            if challenge_kind not in ("mfa_challenge", "add_phone"):
                break
            if challenge_kind in seen_challenges:
                raise RuntimeError(f"认证挑战重复出现，停止自动循环: {challenge_kind}")
            seen_challenges.add(challenge_kind)
            if challenge_kind == "add_phone":
                from core.protocol_phone import verify_registration_phone
                validate_result = verify_registration_phone(session)
            else:
                saved_password, validate_result = continue_existing_login(
                    session, email, "mfa_challenge", validate_result,
                )
                registration_password = registration_password or saved_password
            page_type, otp_continue_url = step_info(validate_result)
        logger.info(
            f"[步骤10] 后续分支判断: page_type={page_type or '空'}, "
            f"has_continue_url={bool(otp_continue_url)}"
        )

        # ==================== 阶段5/6: 完成注册或直接 OAuth 回调 ====================
        next_kind = landing_step(otp_continue_url) if otp_continue_url else "unknown"
        is_about_you = next_kind == "about_you" or (not otp_continue_url and page_type in ("about_you", "about-you"))
        if next_kind == "external_url" or (page_type == "external_url" and not is_about_you):
            if not otp_continue_url:
                raise RuntimeError(f"OTP external_url 响应缺少可跟随 URL，无法继续: {validate_result}")
            logger.info(f"[注册] OTP 后进入 OAuth 回调分支，跳过 create_account：{email}")
            create_acknowledged = True
            session_info, access_token = _finalize_registration_session(
                session,
                otp_continue_url,
                email,
                callback_referer="https://auth.openai.com/email-verification",
            )
            if getattr(_protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", True):
                from core.chatgpt_bootstrap import authenticated_bootstrap
                authenticated_bootstrap(
                    session,
                    access_token,
                    strict=bool(getattr(_protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
                )
            human_delay("post_auth")
        else:
            if not is_about_you:
                raise RuntimeError(f"OTP 后续页面类型未知，不应盲目 create_account: page_type={page_type or '空'}")
            # 只接受真实 about-you 路径，不能被查询参数里的 about-you 字样误导。
            about_url = otp_continue_url if next_kind == "about_you" else None
            navigate_about_you(session, about_url)
            human_delay("navigate")

            # 步骤11: 获取 Sentinel Token（oauth_create_account）
            sentinel_resp_11 = request_sentinel_token(session, "oauth_create_account")
            sentinel_header_11, so_header_11 = build_sentinel_header(session, sentinel_resp_11, "oauth_create_account")
            human_delay("challenge")

            human_delay("form")

            # 步骤12: 提交用户信息，完成注册
            try:
                create_result = create_account(session, name, birthday, sentinel_header_11, so_header_11)
            except Exception as create_exc:
                recovered_url = recover_registration_disallowed(session, email, create_exc)
                create_result = {"continue_url": recovered_url}
            create_acknowledged = True

            logger.info(f"[注册] 资料/认证阶段已完成：{email}，继续完成 OAuth 回调")
            human_delay("post_auth")

            # 步骤12.5: 跟随 create_account 返回的 continue_url 完成 OAuth 回调
            _, continue_url = step_info(create_result)
            if not continue_url:
                continue_url = select_registration_workspace(
                    session, referer="https://auth.openai.com/about-you",
                )

            # 步骤13: 拉 /api/auth/session 提取 accessToken
            session_info, access_token = _finalize_registration_session(session, continue_url, email)
            if getattr(_protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", True):
                from core.chatgpt_bootstrap import authenticated_bootstrap
                authenticated_bootstrap(
                    session,
                    access_token,
                    strict=bool(getattr(_protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
                )
            human_delay("post_auth")

        # ==================== 阶段7: 设置 2FA（受 config.ENABLE_2FA 控制）====================
        checkpoint = install_registration_checkpoint(session, email, session_info, access_token, registration_password)
        saved_password = getattr(session, "_registration_password", None)
        if not registration_password and isinstance(saved_password, str):
            registration_password = saved_password
        known_secret = getattr(session, "_registration_totp_secret", None)
        check_stop_requested()
        totp_secret = known_secret if isinstance(known_secret, str) and known_secret else None
        initial_pending = getattr(session, "_pending_totp_enrollment", None)
        checkpoint(totp_secret=totp_secret, pending=initial_pending if isinstance(initial_pending, dict) else None)
        credentials_saved = True
        if _twofa_cfg.ENABLE_2FA and not totp_secret:
            # 优先复用注册登录态；仅需要时回落邮箱重认证。
            try:
                totp_secret = setup_2fa(session, email, access_token=access_token, prefer_inline=True)
            except StopRequested:
                raise
            except Exception as exc:
                logger.error(f"2FA 设置失败: {exc}")
                logger.debug("2FA 错误详情:", exc_info=True)
                logger.warning("将继续保存账号信息（不含 TOTP secret），可后续手动设置")
        else:
            logger.debug("已跳过 2FA 设置 (config.ENABLE_2FA=False)")

        confirmed_secret = getattr(session, "_registration_totp_secret", None)
        if isinstance(confirmed_secret, str) and confirmed_secret:
            totp_secret = confirmed_secret
        pending = getattr(session, "_pending_totp_enrollment", None)
        checkpoint(totp_secret=totp_secret, pending=pending if isinstance(pending, dict) else None)

        # 重认证可能已刷新 token，即使后续 enroll 失败也应保存新 session，而非旧 token。
        refreshed = getattr(session, "_latest_authenticated_session", None)
        if isinstance(refreshed, dict) and isinstance(refreshed.get("accessToken"), str) and refreshed["accessToken"]:
            user = refreshed.get("user")
            refreshed_email = user.get("email") if isinstance(user, dict) else None
            if refreshed_email and str(refreshed_email).strip().casefold() != str(email).strip().casefold():
                raise RuntimeError("重认证 session 邮箱与注册目标不匹配，停止保存账号")
            session_info, access_token = refreshed, refreshed["accessToken"]

        # ==================== 阶段 7.5: Codex OAuth（注册成功→拿回调/CPA凭证）====================
        # 用全新干净 session 从头登录该邮箱，走 邮箱OTP→手机短信验证(接码)→选workspace
        # →拿 code 的标准路径（不复用注册 session，避免撞 choose-an-account）。
        # 产出：
        #   1) codex_result["callback_url"]  命中 redirect_uri 的整条 Location（携带 code/state）
        #   2) codex_result["file_path"]     CPA 可直接导入的 codex-{email}.json
        codex_result = {"status": "skipped", "ok": False, "message": "未触发"}
        check_stop_requested()
        try:
            from core.codex_oauth import run_codex_oauth
            codex_result = run_codex_oauth(email)
        except StopRequested:
            raise
        except Exception as exc:
            codex_result = {
                "status": "failed",
                "ok": False,
                "message": f"{type(exc).__name__}: {str(exc)[:180]}",
            }

        if codex_result.get("ok"):
            logger.info(
                f"[Codex] 成功：{email}，file={codex_result.get('file_path')}，"
                f"callback={codex_result.get('callback_url')}"
            )
        elif codex_result.get("status") == "skipped":
            logger.info(f"[Codex] 跳过：{email}，原因={codex_result.get('message')}")
        else:
            logger.warning(
                f"[Codex] 失败：{email}，原因={codex_result.get('message')}"
            )

        # ==================== 阶段8: 持久化账号 ====================
        check_stop_requested()
        # 目标在后置授权结束后再读 session；仅更新有效同邮箱凭证，读取失败不丢旧凭证。
        try:
            latest = fetch_session(session)
        except StopRequested:
            raise
        except Exception:
            logger.warning("[注册] 最终 session 可选刷新失败，保留已确认凭证")
        else:
            if isinstance(latest, dict) and isinstance(latest.get("accessToken"), str) and latest["accessToken"]:
                latest_user = latest.get("user")
                latest_email = latest_user.get("email") if isinstance(latest_user, dict) else None
                if latest_email and str(latest_email).strip().casefold() != str(email).strip().casefold():
                    raise RuntimeError("最终 session 邮箱与注册目标不匹配，停止保存账号")
                session_info, access_token = latest, latest["accessToken"]
                session._latest_authenticated_session = latest
                pending = getattr(session, "_pending_totp_enrollment", None)
                checkpoint(totp_secret=totp_secret, pending=pending if isinstance(pending, dict) else None)
        pending_totp = getattr(session, "_pending_totp_enrollment", None)
        from core.email_provider import resolve_email_source
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=session.proxy or None,
            batch_dir=batch_dir,
            auto_twofa=False,  # 本任务已经尝试过；不能在保存时又并发启动一次绑定。
            extra={
                "registration_stage": "complete",
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "device_id": session.device_id,
                "sentinel_sid": getattr(session, "sentinel_sid", None),
                "browser_profile": getattr(session, "browser_profile", None),
                "registration_password": registration_password,
                # 激活结果不明确时保留恢复材料，不将未确认 secret 标成已启用。
                "pending_totp_enrollment": pending_totp if isinstance(pending_totp, dict) else None,
                "codex": codex_result,
            },
        )

        logger.info(f"[完成] {email}，账号ID={account_id}，Token={access_token[:16]}...")

        # ==================== 阶段9: 后置自动触发 flow ====================
        # 只有走完回调、拿到 token 并保存成功的账号，才会触发 flow。
        # flow 请求不影响账号保存状态，但会记录结果并参与批量统计。
        flow_result = {"status": "skipped", "ok": False, "message": "未触发"}
        try:
            from core.flow_trigger import trigger_flow
            flow_result = trigger_flow(access_token)
        except StopRequested:
            raise
        except Exception as exc:
            flow_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}

        if flow_result.get("ok"):
            logger.info(
                f"[Flow] 成功：{email}，HTTP={flow_result.get('http_status')}, "
                f"flow_id={flow_result.get('flow_id') or '未解析'}"
            )
        elif flow_result.get("status") == "skipped":
            logger.info(f"[Flow] 跳过：{email}，原因={flow_result.get('message')}")
        else:
            logger.warning(
                f"[Flow] 失败：{email}，HTTP={flow_result.get('http_status') or '无'}, "
                f"原因={flow_result.get('message')}"
            )

        logger.debug(f"[完成] TOTP Secret: {totp_secret or '(未设置)'}")

        # 注册任务的成功判定：账号本身(注册+token)+Codex 授权都成功才算 success。
        # Codex 失败时账号仍保存（token 拿到了、有补跑机会），但任务状态标失败，
        # 让 WebUI 任务表能清楚区分"完整成功"和"差 Codex"两种结果。
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        task_success = codex_ok
        task_error = None
        if not task_success:
            task_error = f"Codex 未完成: {codex_result.get('message', '未知')}"
            logger.warning(f"[任务结果] {email} 账号已保存但任务标失败，原因: {task_error}")

        return {"success": task_success, "email": email, "account_id": account_id,
                "access_token": access_token, "totp_secret": totp_secret,
                "flow": flow_result, "codex": codex_result,
                "error": task_error}

    except Exception as e:
        logger.error(f"[失败] {email}: {type(e).__name__}: {e}")
        logger.debug("详细错误信息:", exc_info=True)
        # 邮箱状态回收策略，三种情况：
        #   1. 账号已废（account_deactivated 等）：邮箱素材本身不可用，标 failed 直接剔除。
        #   2. 创建接口通过后失败：远端已消耗这个邮箱，直接废弃，避免重复注册。
        #   3. 创建接口通过前的普通失败：邮箱还可以下次继续尝试，放回 available。
        from core.openai_auth import AccountUnusableError
        account_dead = isinstance(e, AccountUnusableError)
        try:
            if email:
                from core.email_provider import release_email
                if account_dead:
                    src = release_email(
                        email, status="failed",
                        note=f"账号已废弃，邮箱不可用: {str(e)[:180]}",
                    )
                    logger.warning(f"[邮箱:{src}] {email} 账号已废弃，标记为 failed，不再重新注册")
                elif credentials_saved or password_saved:
                    logger.info("[注册] 已保存可恢复凭证，保留账号和邮箱状态供补跑：%s", email)
                elif create_acknowledged:
                    src = release_email(
                        email, status="failed",
                        note=f"创建接口已通过但后续失败，已废弃: {str(e)[:180]}",
                    )
                    logger.warning(f"[邮箱:{src}] {email} 已创建但后续失败，标记为 failed，不再重新注册")
                else:
                    src = release_email(email, status="available", note=f"上次失败: {str(e)[:180]}")
                    logger.info(f"[邮箱:{src}] {email} 已恢复 available")
        except Exception:
            pass
        if isinstance(e, StopRequested):
            raise
        return {"success": False, "email": email, "error": str(e)}
    finally:
        # 登录态预热是同步的；Codex 使用独立会话，此处可释放连接池/代理中继。
        try:
            close_browser_session(session)
        except Exception as close_exc:
            logger.warning("[注册] 释放协议会话失败: %s", type(close_exc).__name__)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="ChatGPT 协议注册 CLI")
    parser.add_argument("-n", "--count", type=int, default=1, help="连续注册数量，默认 1")
    parser.add_argument("--workers", type=int, default=1, help="并发注册线程数，默认 1（串行）")
    parser.add_argument("--delay", type=float, default=0, help="每次注册结束后的间隔秒数")
    parser.add_argument("--continue-on-fail", action="store_true", help="单个账号失败后继续注册下一个")
    parser.add_argument("--verbose", action="store_true", help="显示详细步骤日志和错误堆栈")
    args = parser.parse_args()
    configure_logging(args.verbose)

    if args.count < 1:
        logger.error("注册数量必须大于 0")
        sys.exit(1)

    if args.workers < 1:
        logger.error("并发线程数必须大于 0")
        sys.exit(1)

    if args.count > 1 and REGISTER_EMAIL:
        logger.error("config.REGISTER_EMAIL 已固定邮箱，不适合批量注册；请留空后再使用 --count")
        sys.exit(1)

    if args.workers > 1 and not _email_cfg.USE_EMAIL_SERVICE:
        logger.error("多线程注册需要启用 Outlook 自动取件；请开启 USE_EMAIL_SERVICE 或改用 --workers 1")
        sys.exit(1)

    if args.workers > args.count:
        logger.info(f"[批量] 并发线程数 {args.workers} 大于目标数量，已按 {args.count} 个任务执行")
        args.workers = args.count

    if args.workers > 1:
        batch_dir = create_batch_archive_dir(args.count, args.workers)
        logger.info("[批量] 账号数据将直接写入 SQLite")
        results = run_parallel_batch(args.count, args.workers, args.delay, args.continue_on_fail, batch_dir)
    else:
        batch_dir = create_batch_archive_dir(args.count, args.workers)
        logger.info("[批量] 账号数据将直接写入 SQLite")
        results = run_serial_batch(args.count, args.delay, args.continue_on_fail, batch_dir)

    success_count = sum(1 for r in results if _is_success(r))
    flow_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("flow"), dict) and r["flow"].get("ok")
    )
    flow_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("flow"), dict)
        and r["flow"].get("status") == "failed"
    )
    flow_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("flow"), dict)
        and r["flow"].get("status") == "skipped"
    )
    codex_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("codex"), dict) and r["codex"].get("ok")
    )
    codex_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("codex"), dict)
        and r["codex"].get("status") == "failed"
    )
    codex_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("codex"), dict)
        and r["codex"].get("status") == "skipped"
    )
    logger.info(f"[批量] 完成：成功 {success_count} / 尝试 {len(results)} / 目标 {args.count}")
    if success_count:
        logger.info(
            f"[批量] Flow：成功 {flow_success_count} / 失败 {flow_failed_count} / 跳过 {flow_skipped_count}"
        )
        logger.info(
            f"[批量] Codex：成功 {codex_success_count} / 失败 {codex_failed_count} / 跳过 {codex_skipped_count}"
        )
    sys.exit(0 if success_count == args.count else 1)


def run_one_batch_item(index: int, total: int, batch_dir=None) -> dict:
    """执行批量注册中的一个任务，返回结构化结果。"""
    logger.info(f"[批量] 开始第 {index + 1}/{total} 个注册")
    try:
        email, name, birthday = prepare_registration_inputs()
        return run_registration(
            email=email,
            name=name,
            birthday=birthday,
            batch_dir=batch_dir,
            # proxy 不传 → BrowserSession 会从 PROXY_POOL 随机抽
        )
    except Exception as exc:
        logger.error(f"[批量] 第 {index + 1} 个注册准备阶段失败: {type(exc).__name__}: {exc}")
        logger.debug("准备阶段错误详情:", exc_info=True)
        return {"success": False, "error": str(exc)}


def run_serial_batch(count: int, delay: float, continue_on_fail: bool, batch_dir=None) -> list[dict]:
    """按原有串行方式执行批量注册。"""
    results = []
    for index in range(count):
        result = run_one_batch_item(index, count, batch_dir)
        results.append(result)
        if not _is_success(result) and not continue_on_fail:
            logger.error("[批量] 当前账号失败，已停止。需要继续跑可加 --continue-on-fail")
            break

        if delay > 0 and index < count - 1:
            logger.info(f"[批量] 等待 {delay} 秒后继续")
            time.sleep(delay)
    return results


def run_parallel_batch(
    count: int,
    workers: int,
    delay: float,
    continue_on_fail: bool,
    batch_dir=None,
) -> list[dict]:
    """使用线程池并发执行批量注册。"""
    logger.info(f"[批量] 启用多线程注册：目标 {count}，并发 {workers}")
    if delay > 0:
        logger.info(f"[批量] 并发模式下 --delay={delay} 表示提交任务之间的错峰间隔")

    results: list[dict] = []
    future_to_index = {}
    next_index = 0
    stop_submitting = False

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal next_index
        if stop_submitting or next_index >= count:
            return False
        future = executor.submit(run_one_batch_item, next_index, count, batch_dir)
        future_to_index[future] = next_index
        next_index += 1
        if delay > 0 and next_index < count:
            time.sleep(delay)
        return True

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reg-cli") as executor:
        while len(future_to_index) < workers and submit_next(executor):
            pass

        while future_to_index:
            done, _ = wait(future_to_index, return_when=FIRST_COMPLETED)
            for future in done:
                index = future_to_index.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"[批量] 第 {index + 1}/{count} 个注册线程异常: {type(exc).__name__}: {exc}")
                    logger.debug("线程错误详情:", exc_info=True)
                    result = {"success": False, "error": str(exc)}
                results.append(result)

                if not _is_success(result) and not continue_on_fail:
                    stop_submitting = True
                    logger.error("[批量] 当前账号失败，已停止提交新任务。已开始的任务会继续跑完。")

            while len(future_to_index) < workers and submit_next(executor):
                pass

    return results


if __name__ == "__main__":
    main()
