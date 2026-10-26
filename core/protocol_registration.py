# -*- coding: utf-8 -*-
"""协议注册的条件分支；复用 BrowserSession，兼容 curl_cffi 与 wreq。

只在服务端返回对应状态时执行登录、重发或 workspace 选择；不向正常
新注册链路无条件追加诊断请求，也不在会话建立后更换代理/指纹。
"""
import base64
import json
import logging
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

from core import openai_auth as auth
from core.cookie_scope import cookies_for_url

logger = logging.getLogger(__name__)
_AUTH = "https://auth.openai.com"


def _mail_context(source: str, email: str) -> dict:
    from core.email_provider import get_email_context_metadata
    return get_email_context_metadata(email, source)


def checkpoint_registration_password(session, email: str, password: str) -> int:
    """user/register 确认后立即保存密码；取件失败也能恢复，不冒充已拿 token。"""
    from core import db
    from core.email_provider import resolve_email_source
    source = resolve_email_source(email)
    extra = {"registration_password": password, "registration_stage": "password_registered",
             "device_id": session.device_id, "browser_profile": getattr(session, "browser_profile", None),
             "sentinel_sid": getattr(session, "sentinel_sid", None)}
    metadata = _mail_context(source, email)
    if metadata:
        extra["email_service"] = metadata
    return db.checkpoint_registration_account(email=email, access_token="", email_source=source,
                                             proxy_used=session.proxy or None, extra=extra)


def install_registration_checkpoint(session, email: str, session_info: dict,
                                    access_token: str, password: str | None):
    """在有 token 后持久化密码/secret，供独立 Codex 会话读取并避免中断丢失。"""
    from core import db
    from core.email_provider import resolve_email_source
    source = resolve_email_source(email)
    account = db.get_account_by_email(email) or {}
    raw = account.get("extra_json")
    try:
        saved = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    password = password or saved.get("registration_password")
    if isinstance(password, str) and password:
        session._registration_password = password
    secret = account.get("totp_secret")
    if isinstance(secret, str) and secret:
        session._registration_totp_secret = secret
    pending = saved.get("pending_totp_enrollment")
    if isinstance(pending, dict) and pending and not secret:
        session._pending_totp_enrollment = pending
    previous = None

    def checkpoint(*, totp_secret=None, pending=None):
        nonlocal previous
        refreshed = getattr(session, "_latest_authenticated_session", None)
        info = refreshed if isinstance(refreshed, dict) and refreshed.get("accessToken") else session_info
        token = info.get("accessToken") or access_token
        user = info.get("user")
        actual_email = user.get("email") if isinstance(user, dict) else None
        if actual_email and str(actual_email).strip().casefold() != email.strip().casefold():
            raise RuntimeError("凭证检查点 session 邮箱与注册目标不匹配")
        extra = {"user": user, "account": info.get("account"), "expires": info.get("expires"),
                 "device_id": session.device_id, "browser_profile": getattr(session, "browser_profile", None),
                 "sentinel_sid": getattr(session, "sentinel_sid", None),
                 "pending_totp_enrollment": pending,
                 "registration_stage": "totp_ready" if totp_secret else ("totp_pending" if pending else "session_ready")}
        if password:
            extra["registration_password"] = password
        metadata = _mail_context(source, email)
        if metadata:
            extra["email_service"] = metadata
        signature = json.dumps([token, totp_secret, extra], sort_keys=True, default=str)
        if signature == previous:
            return
        db.checkpoint_registration_account(email=email, access_token=token, totp_secret=totp_secret,
                                           extra=extra, email_source=source, proxy_used=session.proxy or None)
        previous = signature

    session._registration_credential_checkpoint = checkpoint
    return checkpoint


def normalize_auth_url(value: str) -> str:
    """认证返回地址仅允许注册涉及的 HTTPS 域名。"""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not value.strip() or any(c in value for c in ("\r", "\n", "\\")):
        raise RuntimeError("认证响应包含无效的跳转 URL")
    url = urljoin(_AUTH + "/", value.strip())
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "https" and parsed.hostname in ("auth.openai.com", "chatgpt.com")
                 and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("认证响应包含非预期的跳转域名")
    return url


def step_info(data: dict) -> tuple[str, str]:
    data = data if isinstance(data, dict) else {}
    page = data.get("page") or {}
    page = page if isinstance(page, dict) else {}
    payload = page.get("payload") if page.get("type") == "external_url" else {}
    payload = payload if isinstance(payload, dict) else {}
    url = next((source.get(key) for source in (data, page, payload)
                for key in ("continue_url", "external_url", "url") if source.get(key)), "")
    return str(page.get("type") or ""), normalize_auth_url(url)


def email_verification_mode(data: dict) -> str:
    """真实响应常把 passwordless 模式放在 page.payload，而不是 page.type。"""
    page = data.get("page") if isinstance(data, dict) else None
    payload = page.get("payload") if isinstance(page, dict) else None
    mode = payload.get("email_verification_mode") if isinstance(payload, dict) else None
    return mode if isinstance(mode, str) else ""


def landing_step(url: str) -> str:
    parsed = urlparse(normalize_auth_url(url))
    path = parsed.path.rstrip("/") or "/"
    if parsed.hostname == "chatgpt.com":
        return "external_url" if path in ("/", "/api/auth/callback/openai") else "unknown"
    if path == "/log-in/password":
        return "login_password"
    if path == "/log-in":
        return "login"
    if path == "/about-you":
        return "about_you"
    if path == "/add-phone":
        return "add_phone"
    if path.startswith("/mfa-challenge/"):
        return "mfa_challenge"
    if path in ("/create-account", "/create-account/password", "/email-verification"):
        # signup 的 email-verification 仍可能是需要补设密码的新账号。
        return "new"
    if (path in ("/authorize/continue", "/workspace", "/consent", "/choose-an-account")
            or path.startswith(("/workspace/", "/consent/", "/sign-in-with-chatgpt/"))):
        return "external_url"
    return "unknown"


def _post(session, path: str, body: dict | None, referer: str, flow: str = "") -> dict:
    auth._check_stop_requested()
    headers = session.get_auth_headers(referer=referer)
    # 与目标注册链路的同源 JSON fetch 形态一致；不修改其它业务共用头。
    headers.pop("origin", None)
    if flow:
        challenge = auth.request_sentinel_token(session, flow)
        token, so = auth.build_sentinel_header(session, challenge, flow)
        headers["openai-sentinel-token"] = token
        if so:
            headers["openai-sentinel-so-token"] = so
    kwargs = {} if body is None else {"data": json.dumps(body)}
    response = session.post(_AUTH + path, headers=headers, **kwargs)
    return auth.parse_auth_step_response(response, path, accepted_statuses=(200, 201))


def resolve_signup_step(session, email: str, landing_url: str) -> tuple[str, dict]:
    """优先使用 authorize 落点；只有无法识别时才补 authorize/continue。"""
    kind = landing_step(landing_url)
    if kind != "unknown":
        return kind, {"continue_url": landing_url}
    data = _post(session, "/api/accounts/authorize/continue", {
        "username": {"value": email, "kind": "email"}, "screen_hint": "signup",
    }, _AUTH + "/create-account", "authorize_continue")
    page, url = step_info(data)
    mode = email_verification_mode(data)
    if page == "email_otp_verification" and mode == "passwordless_signup":
        return "new", data
    if page == "passwordless_login" or mode == "passwordless_login":
        data["_otp_already_sent"] = True
    mapping = {
        "create_account_password": "new", "passwordless_signup": "new",
        "login_password": "login_password", "passwordless_login": "existing_otp",
        "email_otp_verification": "existing_otp", "about_you": "about_you",
        "about-you": "about_you", "external_url": "external_url",
        "mfa_challenge": "mfa_challenge",
        "add_phone": "add_phone",
    }
    kind = mapping.get(page) or (landing_step(url) if url else "unknown")
    if kind == "unknown":
        raise RuntimeError(f"无法识别注册后续状态: page_type={page or '空'}")
    return kind, data


def resend_registration_otp(session, *, existing: bool = False) -> None:
    """先在当前 challenge 内重发；仅状态不支持时回退，不吞网络/限流错误。"""
    paths = ["/api/accounts/email-otp/resend"]
    if not existing:
        paths.append("/api/accounts/passwordless/send-otp")
    for path in paths:
        auth._check_stop_requested()
        headers = session.get_auth_headers(referer=_AUTH + "/email-verification")
        headers.pop("origin", None)
        response = session.post(_AUTH + path, headers=headers)
        if auth._extract_error_code(response) in auth._ACCOUNT_DEAD_CODES:
            auth.parse_auth_step_response(response, "email-otp/resend")
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                if (response.text or "").strip():
                    raise RuntimeError("OTP 重发接口返回非 JSON 内容，未确认发码")
                payload = None
            if payload is not None and not isinstance(payload, dict):
                raise RuntimeError("OTP 重发接口返回非对象 JSON，未确认发码")
            if isinstance(payload, dict) and (payload.get("success") is False or payload.get("error")):
                raise RuntimeError("OTP 重发接口 HTTP 200，但返回业务失败")
            logger.info("[OTP] 已在当前认证状态重发验证码")
            return
        if response.status_code not in (400, 404, 405, 409):
            response.raise_for_status()
            raise RuntimeError(f"OTP 重发返回非预期状态: {response.status_code}")
    # send 会建立新的验证页，调用方必须重新记录时间并丢弃旧码。
    auth.send_email_otp(session)


def continue_existing_login(session, email: str, kind: str, data: dict) -> tuple[str | None, dict]:
    """只用已保存凭证登录，不猜密码；无密码时使用服务端支持的邮箱 OTP。"""
    from core import db
    account = db.get_account_by_email(email) or {}
    extra = account.get("extra_json") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    extra = extra if isinstance(extra, dict) else {}
    # account.password 可能是 Outlook/邮箱池取件密码，不能提交到 ChatGPT 登录接口。
    password = extra.get("registration_password")
    password = password if isinstance(password, str) and password else None
    secret = str(account.get("totp_secret") or "")
    if secret:
        session._registration_totp_secret = secret
    _, url = step_info(data)
    if kind == "login":
        data = _post(session, "/api/accounts/authorize/continue", {
            "username": {"value": email, "kind": "email"}, "screen_hint": "login",
        }, _AUTH + "/log-in", "authorize_continue")
    page, next_url = step_info(data)
    url = next_url or url
    if kind == "login_password" or page == "login_password" or (url and landing_step(url) == "login_password"):
        if password:
            data = _post(session, "/api/accounts/password/verify", {"password": password},
                         _AUTH + "/log-in/password", "authorize_continue")
        else:
            data = _post(session, "/api/accounts/passwordless/send-otp", None,
                         _AUTH + "/log-in/password", "authorize_continue")
            # 部分版本只返回 success，不返回下一步。
            if not any(step_info(data)):
                data = {"page": {"type": "email_otp_verification"},
                        "continue_url": _AUTH + "/email-verification"}
            data["_otp_already_sent"] = True
    page, url = step_info(data)
    if kind == "mfa_challenge" or page == "mfa_challenge" or (url and landing_step(url) == "mfa_challenge"):
        if not secret or not url or landing_step(url) != "mfa_challenge":
            raise RuntimeError("已有账号需要 TOTP，但缺少已保存 secret 或 challenge_id")
        import pyotp
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "mfa-challenge" or not parts[1]:
            raise RuntimeError("MFA challenge_id 无效")
        challenge_id = parts[1]
        data = _post(session, "/api/accounts/mfa/verify", {
            "code": pyotp.TOTP(secret).now(), "type": "totp", "id": challenge_id,
        }, url)
    page, url = step_info(data)
    if not page and not url:
        raise RuntimeError("已有账号登录未返回可继续的认证状态")
    if page == "passwordless_login" or email_verification_mode(data) == "passwordless_login":
        data["_otp_already_sent"] = True
    return password, data


def needs_email_otp(data: dict) -> bool:
    page, url = step_info(data)
    return page in ("email_otp_verification", "passwordless_login", "passwordless_signup") or (
        bool(url) and urlparse(url).hostname == "auth.openai.com"
        and urlparse(url).path.rstrip("/") == "/email-verification")


def workspace_id(session, html: str = "") -> str:
    """从认证 Cookie/SSR 提取 workspace，兼容 JWT 与单段 base64 JSON。"""
    jar = getattr(getattr(session, "session", None), "cookies", None)
    raw_values = []
    try:
        source = cookies_for_url(jar, _AUTH + "/")
        raw_values = [c.value for c in source
                      if c.name == "oai-client-auth-session"]
    except (AttributeError, TypeError):
        pass
    for raw in raw_values:
        for segment in raw.split(".")[:2]:
            try:
                payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("workspace_id"):
                return str(payload["workspace_id"])
            workspaces = payload.get("workspaces")
            for item in workspaces if isinstance(workspaces, list) else []:
                if isinstance(item, dict) and item.get("id"):
                    return str(item["id"])
    # 只读取明确的 workspace 字段，不从任意 HTML 的 id 猜测。
    clean = html.replace('\\"', '"')
    match = re.search(r'"(?:workspace_id|workspaceId)"\s*:\s*"([^"\s]+)"', clean)
    if not match:
        match = re.search(r'"workspaces"\s*:\s*\[\s*\{\s*"id"\s*:\s*"([^"\s]+)"', clean)
    if not match:
        # React Router 的扁平流式数组，不是普通 JSON 对象。
        match = re.search(r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"', clean, re.DOTALL)
    return match.group(1) if match else ""


def select_registration_workspace(session, *, referer: str, html: str = "") -> str:
    referer = normalize_auth_url(referer)
    wid = workspace_id(session, html)
    if not wid and urlparse(referer).path.startswith("/workspace"):
        query = parse_qs(urlparse(referer).query)
        wid = next(iter(query.get("workspace_id") or query.get("id") or []), "")
    if not wid:
        raise RuntimeError("认证流程需要 workspace 选择，但未找到 workspace_id")
    data = _post(session, "/api/accounts/workspace/select", {"workspace_id": wid}, referer)
    _, url = step_info(data)
    if not url:
        raise RuntimeError("workspace/select 未返回 continue_url")
    return url


def select_registration_account(session, *, referer: str, html: str) -> str:
    """选择唯一可用 session；多账号不能靠出现顺序猜测目标账号。"""
    ids = set(re.findall(r'\bus_[A-Za-z0-9]{16,}\b', html or ""))
    if len(ids) != 1:
        raise RuntimeError("账号选择页缺少唯一 session_id，无法自动选择")
    referer = normalize_auth_url(referer)
    body = {"session_id": ids.pop()}
    auth._check_stop_requested()
    headers = session.get_auth_headers(referer=referer)
    headers["origin"] = _AUTH
    response = session.post(_AUTH + "/api/accounts/session/select", headers=headers,
                            data=json.dumps(body), allow_redirects=False)
    if auth._extract_error_code(response) in auth._ACCOUNT_DEAD_CODES:
        auth.parse_auth_step_response(response, "session/select")
    # 仅明确端点不支持时用目标工程的 React Router action；未知结果不能重放选择。
    if response.status_code in (404, 405):
        auth._check_stop_requested()
        headers = session.get_auth_headers(referer=referer)
        headers.update({"content-type": "application/x-www-form-urlencoded",
                        "accept": "application/json, text/html;q=0.9", "origin": _AUTH})
        response = session.post(_AUTH + "/choose-an-account", headers=headers,
                                data=urlencode({"intent": "select", **body}), allow_redirects=False)
    if response.status_code in (302, 303):
        location = response.headers.get("location") or response.headers.get("Location")
        if not location:
            raise RuntimeError("账号选择重定向缺少 Location")
        return normalize_auth_url(urljoin(referer, location))
    data = auth.parse_auth_step_response(response, "session/select", accepted_statuses=(200, 201))
    _, url = step_info(data)
    # 兼容只更新 Cookie 的成功响应；外层只允许同页选择一次。
    return url or normalize_auth_url(referer)


def follow_registration_callback(session, url: str, referer: str) -> str:
    """有限逐跳跟随，消费 ChatGPT 回调；按落点选择账号或 workspace。"""
    start = normalize_auth_url(url)
    progress = getattr(session, "_registration_callback_progress", None)
    if not isinstance(progress, dict) or progress.get("start") != start:
        progress = {"start": start, "current": start, "referer": referer, "selected": set(), "hops": 0}
        session._registration_callback_progress = progress
    current, referer, selected = progress["current"], progress["referer"], progress["selected"]
    for _ in range(16 - progress["hops"]):
        progress.update(current=current, referer=referer, hops=progress["hops"] + 1)
        auth._check_stop_requested()
        host = urlparse(current).hostname
        getter = session.get_chatgpt_navigate_headers if host == "chatgpt.com" else session.get_auth_navigate_headers
        headers = getter(referer=referer, user_initiated=False)
        if host == "chatgpt.com" and path_is_callback(current):
            # 超时也可能已经消费了 code；上层随后只探测 session，不重放。
            session._registration_callback_started = True
        response = session.get(current, headers=headers, allow_redirects=False)
        response.raise_for_status()
        sync = getattr(session, "sync_device_id_from_cookies", None)
        if callable(sync):
            sync(host)
        path = urlparse(current).path
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                raise RuntimeError("OAuth 重定向缺少 Location")
            referer, current = current, normalize_auth_url(urljoin(current, location))
            progress.update(current=current, referer=referer)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"OAuth 回调返回非预期状态: {response.status_code}")
        path = path.rstrip("/") or "/"
        if host == "auth.openai.com" and path == "/add-phone":
            if "add_phone" in selected:
                raise RuntimeError("手机验证后仍返回 add-phone 页面")
            from core.protocol_phone import verify_registration_phone
            result = verify_registration_phone(session)
            _, next_url = step_info(result)
            if not next_url:
                raise RuntimeError("手机验证后缺少继续地址")
            selected.add("add_phone")
            referer, current = current, next_url
            progress.update(current=current, referer=referer)
            continue
        if host == "auth.openai.com" and path == "/choose-an-account":
            if current in selected:
                raise RuntimeError("账号选择后仍返回同一页面")
            selected.add(current)
            referer, current = current, select_registration_account(
                session, referer=current, html=response.text or "")
            progress.update(current=current, referer=referer)
            continue
        if host == "auth.openai.com" and (path in ("/workspace", "/consent")
                or path.startswith(("/workspace/", "/consent/", "/sign-in-with-chatgpt/"))):
            if current in selected:
                raise RuntimeError("workspace 选择后仍返回同一页面")
            selected.add(current)
            referer, current = current, select_registration_workspace(
                session, referer=current, html=response.text or "")
            progress.update(current=current, referer=referer)
            continue
        if host != "chatgpt.com":
            raise RuntimeError(f"OAuth 回调停留在认证页面: {path}")
        observe = getattr(session, "observe_chatgpt_document", None)
        if callable(observe):
            observe(response)
        session._registration_callback_progress = None
        return current
    raise RuntimeError("OAuth 回调重定向次数超过上限")


def path_is_callback(url: str) -> bool:
    return urlparse(url).path.rstrip("/") == "/api/auth/callback/openai"


def recover_registration_disallowed(session, email: str, exc: Exception) -> str:
    """仅 registration_disallowed 尝试一次新授权；其它错误保持原样。"""
    response = getattr(exc, "response", None)
    try:
        payload = response.json() if response is not None else {}
    except (ValueError, AttributeError):
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else ""
    if code != "registration_disallowed" and "registration_disallowed" not in str(exc):
        raise exc
    return reauthorize_registration_session(session, email)


def reauthorize_registration_session(session, email: str) -> str:
    """已完成认证但缺少跳转地址时，有限重新授权；绝不重放先前 OAuth code。"""
    from core.chatgpt_auth import get_csrf_token, signin_openai
    logger.info("[注册] 尝试恢复既有认证会话")
    csrf = get_csrf_token(session)
    authorize = normalize_auth_url(signin_openai(session, csrf, email))
    parsed = urlparse(authorize)
    # 复用已完成 OTP 的认证 Cookie，不强制重新登录；保留其它参数及重复值。
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key != "prompt"]
    authorize = parsed._replace(query=urlencode(query)).geturl()
    # 交给逐跳回调状态机执行；不能先自动跟随，否则 callback 可能被提前消费，
    # 再以首页为未知落点报错，或在网络重试时重复使用 OAuth code。
    return authorize
