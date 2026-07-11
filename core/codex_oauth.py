# -*- coding: utf-8 -*-
"""
注册成功后的 Codex OAuth 授权模块（2026-06-15 改造：全新 session + 接码）。

旧方案"复用注册的已登录 session"会撞 /choose-an-account 卡死（React SPA 解析不出
可提交字段）。新方案改为用**全新干净 session**从头登录，走 OpenAI 标准风控路径，
手机号验证靠接码平台自动收码，当前通过 core.sms_provider 支持 GrizzlySMS 和 L_API.md
定义的本地 L 取号服务。

完整接口链（2026-06-15 浏览器抓包确认，均 POST auth.openai.com，json）：
    1. 提交邮箱   /api/accounts/authorize/continue  {"username":{"kind":"email","value":邮箱}}  带 sentinel(authorize_continue)
    2. 验邮箱码   /api/accounts/email-otp/validate   {"code":"xxx"}                            带 sentinel(authorize_continue)
    3. 提交手机号 /api/accounts/add-phone/send       {"phone_number":"+1xxx","channel":"sms"}  无需 sentinel
    4. 验手机码   /api/accounts/phone-otp/validate   {"code":"xxx"}                            无需 sentinel
    5. 选 workspace /api/accounts/workspace/select   {"workspace_id":"<uuid>"}                  无需 sentinel
       workspace_id 从 oai-client-auth-session cookie（base64 解码）的 workspaces[0].id 取
    6. → 重定向 localhost:1455/auth/callback?code=ac_...，从 Location 抠 code

拿到 code 后换 token / 落盘的逻辑（exchange_codex_token / build_codex_storage /
save_codex_credential）沿用旧实现，未改动。
"""
import base64
import hashlib
import json
import logging
import random
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

# 用模块属性方式访问 config，支持 WebUI 热加载（config.reload_all()）。
# 协议级常量（CLIENT_ID/URL/SCOPE/OUTPUT_DIRNAME）虽然不会改，统一从 _cfg 读，
# 这样 reload 后立即生效，不用再分两套导入。
from config import codex as _cfg
from core.session import BrowserSession
from core.humanize import delay as human_delay
from core.openai_auth import (
    _is_transient_network_error,
    _extract_error_code,
    AccountUnusableError,
    request_sentinel_token,
    build_sentinel_header,
)
from core import sms_provider
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 跟重定向链时的最大跳数，防死循环
_MAX_REDIRECTS = 15

# 网络层临时性错误（代理抖动 / TLS 握手失败 / 重置）重试参数，对齐 openai_auth.follow_authorize
_NET_MAX_ATTEMPTS = 3
_NET_BACKOFF_BASE = 2.0


def _with_net_retry(label: str, fn):
    """
    对临时性网络错误（TLS/代理/超时/重置）做重试包装。
    非临时错误（业务 4xx 等）直接抛。最多 _NET_MAX_ATTEMPTS 次。
    """
    last_exc = None
    for attempt in range(1, _NET_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_transient_network_error(exc):
                raise
            if attempt >= _NET_MAX_ATTEMPTS:
                break
            backoff = _NET_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[Codex] {label} 临时性网络错误 ({type(exc).__name__}: {str(exc)[:120]})，"
                f"{backoff:.1f}s 后重试 (尝试 {attempt}/{_NET_MAX_ATTEMPTS})..."
            )
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"[Codex] {label} 重试耗尽但无异常记录")


def _codex_result(
    *,
    status: str,
    ok: bool = False,
    http_status: int | None = None,
    email: str | None = None,
    file_path: str | None = None,
    callback_url: str | None = None,
    message: str = "",
) -> dict:
    """构造与 flow_trigger._flow_result 同形态的结构化结果。"""
    return {
        "status": status,
        "ok": ok,
        "http_status": http_status,
        "email": email,
        "file_path": file_path,
        "callback_url": callback_url,
        "message": message,
    }


# ============================================================
# PKCE / state（对照 CLIProxyAPI pkce.go）
# ============================================================

def _generate_pkce() -> tuple[str, str]:
    """生成 PKCE 代码对：verifier=base64url(96字节)，challenge=base64url(sha256(verifier))。"""
    verifier_bytes = secrets.token_bytes(96)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _generate_state() -> str:
    """生成 OAuth state 随机串，防 CSRF。"""
    return secrets.token_urlsafe(32)


def _build_authorize_url(state: str, code_challenge: str, prompt: str = "login") -> str:
    """按 CLIProxyAPI openai_auth.go 的参数集拼 Codex 授权 URL。"""
    params = {
        "client_id": _cfg.CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _cfg.CODEX_REDIRECT_URI,
        "scope": _cfg.CODEX_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": prompt,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{_cfg.CODEX_AUTH_URL}?{urlencode(params)}"


# ============================================================
# CPA 管理接口：授权地址由 CPA 生成，成功回调提交给 CPA
# ============================================================

def _codex_auth_url_source() -> str:
    return str(getattr(_cfg, "CODEX_AUTH_URL_SOURCE", "cpa") or "cpa").strip().lower()


def _cpa_management_origin() -> str:
    raw = str(getattr(_cfg, "CPA_MANAGEMENT_URL", "") or "").strip()
    if not raw:
        raise RuntimeError("[Codex][CPA] 尚未配置 CPA_MANAGEMENT_URL")
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise RuntimeError(f"[Codex][CPA] CPA_MANAGEMENT_URL 格式无效: {raw}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"[Codex][CPA] CPA_MANAGEMENT_URL 格式无效: {raw}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _cpa_management_key() -> str:
    key = str(getattr(_cfg, "CPA_MANAGEMENT_KEY", "") or "").strip()
    if not key:
        raise RuntimeError("[Codex][CPA] 尚未配置 CPA_MANAGEMENT_KEY")
    return key


def _cpa_request_json(method: str, path: str, body: dict | None = None) -> dict:
    """调用 CPA 管理接口，兼容 FlowPilot 的 /v0/management/* 协议。"""
    origin = _cpa_management_origin()
    key = _cpa_management_key()
    timeout = int(getattr(_cfg, "CPA_REQUEST_TIMEOUT", 30) or 30)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }
    url = f"{origin}{path}"
    session = curl_requests.Session()
    try:
        resp = session.request(
            method.upper(),
            url,
            headers=headers,
            data=None if body is None else json.dumps(body),
            timeout=timeout,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if resp.status_code < 200 or resp.status_code >= 300:
            msg = ""
            if isinstance(payload, dict):
                msg = payload.get("error") or payload.get("message") or payload.get("detail") or payload.get("reason") or ""
            raise RuntimeError(
                f"[Codex][CPA] 管理接口失败 {method.upper()} {path} status={resp.status_code}: "
                f"{msg or (resp.text or '')[:300]}"
            )
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            session.close()
        except Exception:
            pass


def _first_non_empty(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_state_from_auth_url(auth_url: str) -> str:
    try:
        return parse_qs(urlparse(auth_url).query).get("state", [""])[0]
    except Exception:
        return ""


def _request_cpa_authorize_url() -> dict:
    """从 CPA 生成 Codex OAuth 授权地址；本地不生成 PKCE。"""
    logger.info("[Codex][CPA] 正在通过 CPA 管理接口生成授权地址...")
    payload = _cpa_request_json("GET", "/v0/management/codex-auth-url")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    auth_url = _first_non_empty(
        payload.get("url"),
        payload.get("auth_url"),
        payload.get("authUrl"),
        data.get("url"),
        data.get("auth_url"),
        data.get("authUrl"),
    )
    state = _first_non_empty(
        payload.get("state"),
        payload.get("auth_state"),
        payload.get("authState"),
        data.get("state"),
        data.get("auth_state"),
        data.get("authState"),
        _extract_state_from_auth_url(auth_url),
    )
    if not auth_url.startswith("http"):
        raise RuntimeError(f"[Codex][CPA] CPA 未返回有效 auth_url: {payload}")
    if not state:
        raise RuntimeError("[Codex][CPA] CPA 授权地址缺少 state")
    logger.info(f"[Codex][CPA] 已获取授权地址，state={state[:12]}...")
    logger.info(f"[Codex][CPA] 完整授权地址: {auth_url}")
    return {
        "auth_url": auth_url,
        "state": state,
        "origin": _cpa_management_origin(),
        "raw": payload,
    }


def _is_cpa_callback_retryable(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "status=409" in text
        or "timeout waiting for oauth callback" in text
        or "timeout" in text
        or "timed out" in text
        or "connection" in text
        or "status=429" in text
        or "status=500" in text
        or "status=502" in text
        or "status=503" in text
        or "status=504" in text
    )


def _is_cpa_callback_reauth_error(exc_or_text) -> bool:
    """CPA 收到 callback 后仍 409 timeout，通常需要重新生成授权地址重新跑一轮 OAuth。"""
    text = str(exc_or_text or "").lower()
    return (
        "oauth-callback" in text
        and "status=409" in text
        and "timeout waiting for oauth callback" in text
    ) or (
        "timeout waiting for oauth callback" in text
    )


def _submit_cpa_callback(callback_url: str) -> dict:
    """提交 OAuth callback 给 CPA。

    CPA 偶发会在浏览器已拿到 localhost callback 后仍返回
    “409 Timeout waiting for OAuth callback”，通常是管理端等待/入库的竞态；
    这里按同一个 callback URL 做多次重试，不重新生成授权地址。
    """
    body = {
        "provider": "codex",
        "redirect_url": str(callback_url or "").strip(),
    }
    max_attempts = max(1, int(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 5) or 5))
    base_delay = max(1.0, float(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRY_DELAY", 6) or 6))
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                "[Codex][CPA] 正在提交 OAuth callback 给 CPA（第 %s/%s 次）... callback=%s",
                attempt, max_attempts, str(callback_url or "")
            )
            payload = _cpa_request_json("POST", "/v0/management/oauth-callback", body)
            logger.info("[Codex][CPA] callback 已提交（第 %s 次成功）", attempt)
            return payload
        except Exception as exc:
            last_exc = exc
            retryable = _is_cpa_callback_retryable(exc)
            if attempt >= max_attempts or not retryable:
                logger.warning(
                    "[Codex][CPA] callback 提交失败且不再重试：attempt=%s/%s retryable=%s error=%s",
                    attempt, max_attempts, retryable, exc
                )
                raise
            delay = base_delay * attempt
            logger.warning(
                "[Codex][CPA] callback 提交失败，将在 %.1fs 后重试：attempt=%s/%s error=%s",
                delay, attempt, max_attempts, exc
            )
            time.sleep(delay)
    raise RuntimeError(f"[Codex][CPA] callback 提交失败：{last_exc}")


# ============================================================
# 小工具：判定/解析
# ============================================================

def _is_redirect_uri(location: str) -> bool:
    """判断 Location 是否指向注册的 redirect_uri（localhost:1455/auth/callback）。"""
    try:
        parsed = urlparse(location)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and \
        parsed.hostname in ("localhost", "127.0.0.1") and \
        parsed.port == 1455 and \
        parsed.path == "/auth/callback"


def _extract_code(location: str, state: str) -> str:
    """从 redirect_uri 的 Location 里提取并校验 code。"""
    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    err = (qs.get("error") or [""])[0]
    if err:
        err_desc = (qs.get("error_description") or [""])[0]
        raise RuntimeError(f"[Codex] 授权服务器返回错误: error={err}, desc={err_desc}")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise RuntimeError(f"[Codex] redirect_uri 缺少 code 参数: {location}")
    returned_state = (qs.get("state") or [""])[0]
    if returned_state and returned_state != state:
        raise RuntimeError(
            f"[Codex] state 不匹配（疑似 CSRF）: expected={state[:8]}..., got={returned_state[:8]}..."
        )
    return code


def _decode_jwt_segment(seg: str) -> dict:
    """base64url 解码一个 JWT/cookie 段为 JSON dict（失败返回 {}）。"""
    try:
        padding = "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg + padding))
    except Exception:
        return {}


def _post_json(session: BrowserSession, url: str, payload: dict, referer: str,
               sentinel_header: str | None = None):
    """统一发 /api/accounts/* 的 JSON POST。"""
    headers = session.get_auth_headers(referer=referer)
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    return session.post(url, headers=headers, data=json.dumps(payload), allow_redirects=False)


def _resp_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _response_text(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            parts = []
            def walk(x):
                if isinstance(x, dict):
                    for v in x.values(): walk(v)
                elif isinstance(x, list):
                    for v in x: walk(v)
                elif x is not None:
                    parts.append(str(x))
            walk(data)
            return " ".join(parts)
    except Exception:
        pass
    return str(getattr(resp, 'text', '') or '')


def _phone_failure_reason(text: str, status_code: int | None = None) -> str:
    low = str(text or '').lower()
    if 'whatsapp' in low or 'whats app' in low:
        return 'whatsapp_channel'
    if any(k in low for k in (
        'phone number is not valid', 'invalid phone number', 'invalid phone', 'not a valid phone',
        '号码无效', '手机号无效', '电话号码无效', 'invalid_number', 'invalid_phone',
    )):
        return 'invalid_phone'
    if any(k in low for k in (
        'cannot send', "can't send", 'could not send', "couldn't send", 'unable to send',
        'cannot deliver', 'unable to deliver', 'failed to send', 'send failed',
        '无法发送', '不能发送', '无法向', '发送验证码', '发送短信',
    )):
        return 'delivery_refused'
    if any(k in low for k in ('too many', 'rate limit', 'throttle', 'limited', '频繁', '限流')):
        return 'send_limited'
    if any(k in low for k in ('already used', 'used too many', 'maximum', '上限', '已被使用')):
        return 'phone_used_or_max'
    if status_code and status_code >= 500:
        return 'server_error'
    if status_code and status_code >= 400:
        return 'send_rejected'
    return ''


# ============================================================
# 步骤 0：用全新 session 跟随 Codex authorize URL，建立 auth.openai.com 会话
# ============================================================

def _bootstrap_authorize(
    session: BrowserSession,
    state: str,
    code_challenge: str | None = None,
    auth_url: str | None = None,
) -> None:
    """
    GET Codex authorize URL 并跟随重定向，落到登录页，建立 auth.openai.com cookies
    （含 oai-client-auth-session：内含 Codex 目标 + 后续要用的 workspace 列表）。
    """
    # 默认使用调用方传入的 CPA 授权地址；未传时才走保留的本地 PKCE 生成逻辑。
    if not auth_url:
        if not code_challenge:
            raise RuntimeError("[Codex] 本地生成授权地址需要 code_challenge")
        auth_url = _build_authorize_url(state, code_challenge, prompt="login")
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[Codex] 跟随 Codex authorize URL 建立会话...")
    logger.info(f"[Codex] 完整授权地址: {auth_url}")
    resp = _with_net_retry(
        "bootstrap authorize",
        lambda: session.get(auth_url, headers=headers, allow_redirects=True),
    )
    logger.debug(f"[Codex] authorize 落点: {getattr(resp, 'url', '')}, status={getattr(resp, 'status_code', '')}")


# ============================================================
# 步骤 1：提交邮箱（触发邮箱 OTP 发送）
# ============================================================

def _submit_email(session: BrowserSession, email: str) -> None:
    """POST authorize/continue 提交邮箱，触发 OpenAI 发送邮箱 OTP。带 sentinel。"""
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, _ = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    payload = {"username": {"kind": "email", "value": email}}
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/authorize/continue",
        payload,
        referer="https://auth.openai.com/log-in",
        sentinel_header=sentinel_header,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"[Codex] 提交邮箱失败 status={resp.status_code}: {(resp.text or '')[:300]}"
        )
    logger.info(f"[Codex] 已提交邮箱 {email}，等待邮箱 OTP")


# ============================================================
# 步骤 2：提交邮箱 OTP
# ============================================================

def _submit_email_otp(session: BrowserSession, code: str) -> None:
    """POST email-otp/validate 提交邮箱验证码。带 sentinel(authorize_continue)。"""
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, _ = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/email-otp/validate",
        {"code": code},
        referer="https://auth.openai.com/email-verification",
        sentinel_header=sentinel_header,
    )
    if resp.status_code != 200:
        error_code = _extract_error_code(resp)
        if error_code in ("account_deactivated", "account_deleted", "account_banned"):
            raise AccountUnusableError(
                f"[Codex] 账号已废（{error_code}）status={resp.status_code}: {(resp.text or '')[:200]}",
                error_code=error_code,
            )
        raise RuntimeError(
            f"[Codex] 邮箱 OTP 验证失败 status={resp.status_code}: {(resp.text or '')[:300]}"
        )
    logger.info("[Codex] 邮箱 OTP 验证通过")


# ============================================================
# 步骤 3-4：手机号验证（接码，失败换号重试）
# ============================================================

def _sms_provider_name() -> str:
    """当前接码通道名，仅用于 Codex 流程日志。"""
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _sleep_before_phone_retry(attempt: int, max_retries: int, *, prefix: str = "[Codex]") -> None:
    """换号前随机等待，至少 3 秒，避免连续提交号码过快。"""
    if attempt >= max_retries:
        return
    seconds = random.uniform(3.0, 8.0)
    logger.info(f"{prefix} 换号前随机等待 {seconds:.1f} 秒")
    time.sleep(seconds)


def _do_phone_verification(session: BrowserSession) -> None:
    """
    用接码平台拿号 → add-phone/send 发短信 → 收码 → phone-otp/validate。
    一个号收不到码或被 OpenAI 拒就取消换号，最多 SMS_MAX_RETRIES 次（热加载）。

    实际平台适配在 core.sms_provider：
        - SMS_PROVIDER="grizzly"：GrizzlySMS handler_api.php
        - SMS_PROVIDER="l"：L_API.md 的 /take-phone 和 /fetch-code JSON 接口
    """
    http = sms_provider._http()
    max_retries = _cfg.SMS_MAX_RETRIES
    provider = _sms_provider_name()
    try:
        last_err = None
        for attempt in range(1, max_retries + 1):
            activation_id = None
            try:
                activation_id, phone = sms_provider.acquire_number(http)
                logger.info(
                    f"[Codex] 手机验证尝试 {attempt}/{max_retries}，"
                    f"provider={provider}, activation_id={activation_id}, 号码=+{phone}"
                )

                # 发短信
                send_resp = _post_json(
                    session,
                    "https://auth.openai.com/api/accounts/add-phone/send",
                    {"phone_number": f"+{phone}", "channel": "sms"},
                    referer="https://auth.openai.com/add-phone",
                )
                send_text = _response_text(send_resp)
                send_reason = _phone_failure_reason(send_text, send_resp.status_code)
                if send_resp.status_code not in (200, 204) or send_reason:
                    # 号码无效 / 无法发送 / WhatsApp 通道 / 限流等 → 释放当前号并换号。
                    logger.warning(
                        f"[Codex] add-phone/send 未成功 reason={send_reason or 'unknown'}, "
                        f"status={send_resp.status_code}: {send_text[:240]}，换号重试"
                    )
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_phone_retry(attempt, max_retries)
                    continue

                # 通知平台短信已发出（status=1）
                sms_provider.set_status(activation_id, 1, http=http)

                # 定时轮询接码平台获取短信。wait_for_sms_code 内部按 SMS_POLL_INTERVAL 轮询，
                # 最长等待 SMS_CODE_WAIT；超时立即取消当前号并换号。
                try:
                    logger.info(
                        f"[Codex] 短信已发送，开始轮询验证码 activation_id={activation_id}, "
                        f"wait={_cfg.SMS_CODE_WAIT}s, interval={_cfg.SMS_POLL_INTERVAL}s"
                    )
                    sms_code = sms_provider.wait_for_sms_code(activation_id, http)
                except sms_provider.SmsCodeTimeout:
                    logger.warning(f"[Codex] 号码 +{phone} 在 {_cfg.SMS_CODE_WAIT}s 内未收到短信，取消换号")
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_phone_retry(attempt, max_retries)
                    continue

                # 验手机码
                val_resp = _post_json(
                    session,
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    {"code": sms_code},
                    referer="https://auth.openai.com/phone-verification",
                )
                if val_resp.status_code != 200:
                    val_text = _response_text(val_resp)
                    val_reason = _phone_failure_reason(val_text, val_resp.status_code) or 'code_rejected'
                    logger.warning(
                        f"[Codex] phone-otp/validate 失败 reason={val_reason}, status={val_resp.status_code}: "
                        f"{val_text[:240]}，换号重试"
                    )
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_phone_retry(attempt, max_retries)
                    continue

                # 成功
                sms_provider.complete(activation_id, http)
                logger.info("[Codex] 手机号验证通过")
                return

            except sms_provider.SmsNoBalanceError:
                # 余额不足，重试无意义，直接抛
                raise
            except sms_provider.SmsProviderError as exc:
                last_err = exc
                logger.warning(f"[Codex] 接码尝试 {attempt} 失败：{exc}")
                if activation_id:
                    sms_provider.cancel(activation_id, http)
                _sleep_before_phone_retry(attempt, max_retries)
                continue

        raise RuntimeError(
            f"[Codex] 手机号验证重试 {max_retries} 次仍失败（provider={provider}）"
            + (f"，最后错误：{last_err}" if last_err else "")
        )
    finally:
        http.close()


# ============================================================
# 步骤 5：选 workspace → 拿 callback code
# ============================================================

def _get_workspace_id(session: BrowserSession) -> str:
    """
    从 oai-client-auth-session cookie 解出 workspaces[0].id。
    cookie 形如 base64payload.sig.sig，取第一段 base64 解码后的 JSON。
    """
    raw = None
    try:
        # curl_cffi cookies
        for c in session.session.cookies.jar:
            if c.name == "oai-client-auth-session":
                raw = c.value
                break
    except Exception:
        pass
    if not raw:
        # 退而求其次：从 cookies 字典拿
        try:
            raw = session.session.cookies.get("oai-client-auth-session")
        except Exception:
            raw = None
    if not raw:
        raise RuntimeError("[Codex] 找不到 oai-client-auth-session cookie，无法取 workspace_id")

    payload = _decode_jwt_segment(raw.split(".")[0])
    workspaces = payload.get("workspaces") or []
    if not workspaces:
        raise RuntimeError(f"[Codex] cookie 里无 workspaces 字段: keys={list(payload.keys())}")
    wid = workspaces[0].get("id")
    if not wid:
        raise RuntimeError(f"[Codex] workspaces[0] 无 id: {workspaces[0]}")
    logger.info(f"[Codex] workspace_id={wid}")
    return wid


def _select_workspace_and_get_callback(session: BrowserSession, state: str) -> str:
    """
    POST workspace/select，然后跟随后续重定向/响应里的 URL 直到命中 localhost:1455 callback。
    返回完整 callback URL（含 code）。
    """
    wid = _get_workspace_id(session)
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/workspace/select",
        {"workspace_id": wid},
        referer="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    )

    # 1) 直接带 Location 头命中 callback
    loc = resp.headers.get("location") or resp.headers.get("Location")
    if loc and _is_redirect_uri(loc):
        return loc

    # 2) 响应 JSON 里给了下一步 URL（continue_url / redirect_url / url / next）
    data = _resp_json(resp)
    next_url = None
    for key in ("redirect_url", "continue_url", "url", "next", "location"):
        v = data.get(key)
        if isinstance(v, str) and v:
            next_url = v
            break

    # 3) 没给 URL 但有 Location（非 callback）→ 从 Location 起跟
    if not next_url and loc:
        next_url = loc

    if not next_url:
        raise RuntimeError(
            f"[Codex] workspace/select 后找不到下一跳 URL: status={resp.status_code}, "
            f"body={(resp.text or '')[:300]}"
        )

    # 跟随重定向链直到命中 callback
    return _follow_until_callback(session, next_url, state)


def _follow_until_callback(session: BrowserSession, url: str, state: str) -> str:
    """从给定 URL 起逐跳跟随，命中 localhost:1455 callback 时返回其 Location。"""
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    for hop in range(_MAX_REDIRECTS):
        if _is_redirect_uri(url):
            return url
        headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/")
        resp = session.get(url, headers=headers, allow_redirects=False)
        loc = resp.headers.get("location") or resp.headers.get("Location")
        logger.debug(f"[Codex] callback 跟随 hop {hop}: status={getattr(resp,'status_code','')}, location={loc}")
        if loc is None:
            raise RuntimeError(
                f"[Codex] 跟随中断，未命中 callback: url={url}, "
                f"status={getattr(resp,'status_code','')}, body={(resp.text or '')[:200]}"
            )
        if _is_redirect_uri(loc):
            return loc
        url = loc if loc.startswith("http") else ("https://auth.openai.com" + loc)
    raise RuntimeError(f"[Codex] 跟随 callback 超过 {_MAX_REDIRECTS} 跳")


# ============================================================
# 换 token（对照 CLIProxyAPI ExchangeCodeForTokensWithRedirect）—— 未改动
# ============================================================

def exchange_codex_token(session: BrowserSession, code: str, code_verifier: str) -> dict:
    """用 authorization code 换 token。"""
    data = {
        "grant_type": "authorization_code",
        "client_id": _cfg.CODEX_CLIENT_ID,
        "code": code,
        "redirect_uri": _cfg.CODEX_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    base = session._get_common_headers()
    base.update(headers)
    headers = base

    logger.info("[Codex] 用 authorization code 换 token...")
    resp = session.post(_cfg.CODEX_TOKEN_URL, headers=headers, data=urlencode(data))
    http_status = resp.status_code
    if http_status != 200:
        raise RuntimeError(
            f"[Codex] 换 token 失败 status={http_status}: {(resp.text or '')[:300]}"
        )
    token_resp = resp.json()
    if not token_resp.get("access_token"):
        raise RuntimeError(f"[Codex] token 响应缺少 access_token: {token_resp}")
    logger.info(
        f"[Codex] 换 token 成功，expires_in={token_resp.get('expires_in')}, "
        f"access_token={token_resp['access_token'][:16]}..."
    )
    return token_resp


# ============================================================
# 解析 id_token / 落盘 —— 未改动
# ============================================================

def _parse_id_token(id_token: str) -> dict:
    """base64 解码 JWT payload（不验签），抽 email / account_id / plan_type。"""
    if not id_token:
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        claims = _decode_jwt_segment(parts[1])
    except Exception as exc:
        logger.warning(f"[Codex] id_token 解析失败: {exc}")
        return {}

    auth_claim = claims.get("https://api.openai.com/auth", {}) or {}
    profile_claim = claims.get("https://api.openai.com/profile", {}) or {}
    # OpenAI 新版 id_token 的 email 在顶层 claim；旧版/CLIProxyAPI 实现里在 profile_claim。
    # 顶层优先，否则回退 profile_claim，避免落盘的 codex-邮箱.json 里 email 字段为空。
    email_value = claims.get("email") or profile_claim.get("email", "")
    return {
        "email": email_value,
        "account_id": auth_claim.get("chatgpt_account_id", ""),
        "plan_type": auth_claim.get("chatgpt_plan_type", ""),
    }


def build_codex_storage(token_resp: dict, id_claims: dict) -> dict:
    """组装 CLIProxyAPI CodexTokenStorage JSON 结构。"""
    expires_in = token_resp.get("expires_in", 0) or 0
    expired_dt = datetime.now(timezone.utc) + _timedelta_seconds(expires_in)
    last_refresh_dt = datetime.now(timezone.utc)
    return {
        "id_token": token_resp.get("id_token", ""),
        "access_token": token_resp.get("access_token", ""),
        "refresh_token": token_resp.get("refresh_token", ""),
        "account_id": id_claims.get("account_id", ""),
        "last_refresh": last_refresh_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": id_claims.get("email", ""),
        "type": "codex",
        "expired": expired_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _timedelta_seconds(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=int(seconds))


def _credential_file_name(email: str, plan_type: str) -> str:
    """对照 CLIProxyAPI filename.go：无 plan→codex-{email}.json，否则带 plan 后缀。"""
    email = (email or "").strip()
    plan = (plan_type or "").strip().lower()
    if plan == "":
        return f"codex-{email}.json"
    return f"codex-{email}-{plan}.json"


def save_codex_credential(storage: dict, email: str, plan_type: str) -> Path:
    """落盘到 {PROJECT_ROOT}/{CODEX_OUTPUT_DIRNAME}/codex-{email}.json。"""
    out_dir = _PROJECT_ROOT / _cfg.CODEX_OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = _credential_file_name(email, plan_type)
    path = out_dir / fname
    path.write_text(
        json.dumps(storage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _extract_cpa_auth_json(payload: dict) -> dict | None:
    """
    尝试从 CPA oauth-callback 响应里提取完整授权文件。
    不同 CPA 版本字段名可能不同；只要看起来是 codex auth json 就落本地。
    """
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("auth_json"),
        payload.get("authJson"),
        payload.get("auth"),
        payload.get("auth_file"),
        payload.get("authFile"),
        payload.get("file"),
        payload.get("data"),
    ]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates.extend([
        data.get("auth_json"),
        data.get("authJson"),
        data.get("auth"),
        data.get("auth_file"),
        data.get("authFile"),
        data.get("file"),
    ])
    for item in candidates:
        if isinstance(item, dict) and (
            item.get("type") == "codex"
            or item.get("access_token")
            or item.get("refresh_token")
            or item.get("id_token")
        ):
            return item
    return None


def _save_cpa_local_record(
    *,
    email: str,
    callback_url: str,
    auth_url: str,
    state: str,
    submit_payload: dict,
) -> Path | None:
    """
    本地记录 CPA 授权结果：
      1) 如果 CPA 返回完整 auth json，保存为可用 codex-邮箱[-plan].json；
      2) 否则按配置保存 callback 提交回执，便于追踪 CPA 侧授权文件。
    """
    auth_json = _extract_cpa_auth_json(submit_payload)
    if auth_json:
        effective_email = auth_json.get("email") or email
        plan = auth_json.get("plan_type") or auth_json.get("chatgpt_plan_type") or ""
        return save_codex_credential(auth_json, effective_email, plan)

    if not bool(getattr(_cfg, "CPA_SAVE_CALLBACK_RECEIPT", True)):
        return None

    out_dir = _PROJECT_ROOT / _cfg.CODEX_OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_email = (email or "unknown").strip().replace("/", "_").replace("\\", "_")
    path = out_dir / f"codex-{safe_email}-cpa-callback.json"
    record = {
        "type": "codex_cpa_callback",
        "email": email,
        "state": state,
        "auth_url": auth_url,
        "callback_url": callback_url,
        "cpa_management_origin": _cpa_management_origin(),
        "cpa_submit_response": submit_payload,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "授权地址由 CPA 生成；callback 已提交给 CPA。若 CPA 响应未包含 token，本文件为本地回执记录。",
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ============================================================
# 入口
# ============================================================

def run_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    _cpa_reauth_round: int = 1,
) -> dict:
    """
    注册成功后的 Codex OAuth 授权入口（全新 session + 接码方案）。

    不复用注册的 session：内部新建干净 BrowserSession，从头登录该邮箱，
    走 邮箱 OTP → 手机短信验证 → 选 workspace → 拿 code → 换 token → 落盘。

    Args:
        email: 已注册成功的账号邮箱
        otp_provider: 邮箱 OTP 获取回调 fn(email, after_ts)->code，默认用 wait_for_otp
        proxy: 代理（不传从 PROXY_POOL 抽）
        force: True 时跳过 ENABLE_CODEX_AUTO 开关限制，供手动补跑使用

    Returns:
        结构化结果 dict。任何异常都被吞掉转 status=failed，不向上抛，不影响注册主流程。
    """
    if not force and not _cfg.ENABLE_CODEX_AUTO:
        return _codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return _codex_result(status="skipped", message="email 为空")

    # Codex OAuth 本身也支持 RoxyBrowser 指纹浏览器驱动。
    # protocol：原纯协议；roxy：用 Roxy 跑页面并捕获 localhost callback。
    try:
        from config import codex as _codex_cfg
        from config import roxybrowser as _roxy_cfg
        oauth_driver = str(getattr(_codex_cfg, "CODEX_OAUTH_DRIVER", "protocol") or "protocol").strip().lower()
        if oauth_driver == "same_as_registration":
            oauth_driver = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
        if oauth_driver in ("roxy", "roxybrowser", "fingerprint", "browser"):
            from core.roxy_codex_oauth import run_roxy_codex_oauth
            return run_roxy_codex_oauth(email, otp_provider=otp_provider, proxy=proxy, force=True)
        if oauth_driver in ("cloak", "cloakbrowser"):
            from config import cloakbrowser as _cloak_cfg
            from core.cloakbrowser_driver import build_cloak_driver
            from core.roxy_codex_oauth import run_roxy_codex_oauth
            driver, opened = build_cloak_driver(proxy=proxy)
            try:
                return run_roxy_codex_oauth(
                    email,
                    otp_provider=otp_provider,
                    proxy=proxy,
                    force=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    reuse_existing_profile=True,
                    clear_existing_state=True,
                )
            finally:
                if not bool(getattr(_cloak_cfg, "CLOAK_KEEP_BROWSER_OPEN", False)):
                    try:
                        driver.quit()
                    except Exception:
                        pass
        if oauth_driver not in ("protocol", "api", "http"):
            raise RuntimeError(f"[Codex] 不支持的 CODEX_OAUTH_DRIVER={oauth_driver!r}，可选 protocol / roxy / cloak")
    except ImportError:
        # 没装 selenium / 未提供 roxy 配置时继续走协议模式，保持旧行为。
        pass

    if otp_provider is None:
        from core.email_provider import wait_for_otp as otp_provider

    session = BrowserSession(proxy=proxy)
    try:
        logger.info(f"[Codex] 开始授权（全新 session）：{email}")

        # 1. 授权地址
        #    默认由 CPA 生成（本地不生成 PKCE/state）；local 模式保留旧代码用于兼容。
        auth_source = _codex_auth_url_source()
        cpa_auth = None
        code_verifier = None
        code_challenge = None
        auth_url = None
        if auth_source == "cpa":
            cpa_auth = _request_cpa_authorize_url()
            state = cpa_auth["state"]
            auth_url = cpa_auth["auth_url"]
            logger.info(f"[Codex] 当前使用 CPA 授权地址: {auth_url}")
        elif auth_source == "local":
            code_verifier, code_challenge = _generate_pkce()
            state = _generate_state()
            logger.info("[Codex] 当前使用本地 PKCE 生成授权地址，完整 URL 将在 bootstrap 阶段输出")
        else:
            raise RuntimeError(f"[Codex] 不支持的 CODEX_AUTH_URL_SOURCE={auth_source!r}")

        # 2. 建立会话
        _bootstrap_authorize(session, state, code_challenge, auth_url=auth_url)
        human_delay("navigate")

        # 3. 提交邮箱（触发邮箱 OTP）
        otp_after_ts = time.time()
        _submit_email(session, email)
        human_delay("form")

        # 4. 收邮箱 OTP + 提交
        logger.info(f"[Codex] 等待邮箱 OTP：{email}")
        email_otp = otp_provider(email, after_ts=otp_after_ts)
        logger.info(f"[Codex] 邮箱 OTP 收到：{email_otp}")
        human_delay("otp_input")
        _submit_email_otp(session, email_otp)
        human_delay("api")

        # 5. 手机号验证（接码，自动重试换号）
        _do_phone_verification(session)
        human_delay("post_auth")

        # 6. 选 workspace → 拿 callback code
        callback_url = _select_workspace_and_get_callback(session, state)
        code = _extract_code(callback_url, state)
        logger.info(f"[Codex] 已拿到 authorization code：{code[:24]}...")

        # 7A. CPA 模式：把 callback URL 交给 CPA，由 CPA 持有 verifier 并完成换 token / 写 auth。
        #     本地不再用 code 换 token；仅保存 CPA 返回的授权文件或回调回执。
        if auth_source == "cpa":
            submit_payload = _submit_cpa_callback(callback_url)
            path = _save_cpa_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url or "",
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "CPA callback submitted"
            logger.info(f"[Codex][CPA] 成功：{email}，{msg}，本地记录={path or 'disabled'}")
            return _codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=str(msg),
            )

        # 7B. local 模式：保留旧实现，用本地 verifier 换 token 并保存 CPA 兼容授权文件。
        if not code_verifier:
            raise RuntimeError("[Codex] local 模式缺少 code_verifier")
        token_resp = exchange_codex_token(session, code, code_verifier)

        # 8. 解析 id_token + 落盘
        id_claims = _parse_id_token(token_resp.get("id_token", ""))
        effective_email = id_claims.get("email") or email
        storage = build_codex_storage(token_resp, id_claims)
        path = save_codex_credential(storage, effective_email, id_claims.get("plan_type", ""))

        logger.info(
            f"[Codex] 成功：{effective_email}，plan={id_claims.get('plan_type') or 'unknown'}, "
            f"account_id={id_claims.get('account_id') or 'unknown'}, 已保存到 {path}"
        )
        return _codex_result(
            status="success",
            ok=True,
            email=effective_email,
            file_path=str(path),
            callback_url=callback_url,
            message=f"plan={id_claims.get('plan_type') or 'unknown'}",
        )
    except AccountUnusableError as exc:
        logger.warning(f"[Codex] 账号已废（{exc.error_code}）：{email}")
        return _codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code}）",
        )
    except Exception as exc:
        if _is_cpa_callback_reauth_error(exc) and _cpa_reauth_round < 2:
            logger.warning(
                "[Codex][CPA] callback 返回 Timeout waiting for OAuth callback，重新开启第 %s/2 轮 Codex 授权：%s",
                _cpa_reauth_round + 1, email,
            )
            return run_codex_oauth(
                email,
                otp_provider=otp_provider,
                proxy=proxy,
                force=force,
                _cpa_reauth_round=_cpa_reauth_round + 1,
            )
        logger.warning(f"[Codex] 失败：{email}，{type(exc).__name__}: {str(exc)[:200]}")
        logger.debug("[Codex] 失败详情:", exc_info=True)
        return _codex_result(
            status="failed",
            email=email,
            message=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
