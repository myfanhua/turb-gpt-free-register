from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from .paths import ensure_icloud_on_path

ensure_icloud_on_path()

from registration_core.auth_flow import AuthFlow, AuthResult  # noqa: E402
from registration_core.config import Config  # noqa: E402
from registration_core.mfa_totp_protocol import totp_code_candidates  # noqa: E402

FACTOR_URL_RE = re.compile(r"/mfa-challenge/([0-9a-fA-F]{16,64})")


class MfaLoginError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class LoginSession:
    email: str
    password: str
    totp_secret: str
    auth: AuthFlow
    result: AuthResult
    factor_id: str = ""
    account_id: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def access_token(self) -> str:
        return str(self.result.access_token or "")

    @property
    def session_token(self) -> str:
        return str(self.result.session_token or "")

    @property
    def device_id(self) -> str:
        return str(self.result.device_id or "")

    @property
    def cookie_header(self) -> str:
        return str(self.result.cookie_header or "")


def _mask(value: str, head: int = 8, tail: int = 4) -> str:
    text = str(value or "")
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}...{text[-tail:]}"


def _cookie_value(session, name: str) -> str:
    try:
        jar = getattr(session, "cookies", None)
        if jar is None:
            return ""
        # curl_cffi / requests CookieJar
        if hasattr(jar, "get"):
            val = jar.get(name)
            if val:
                return str(val)
        for c in list(jar):
            if getattr(c, "name", "") == name:
                return str(getattr(c, "value", "") or "")
    except Exception:
        return ""
    return ""


def _parse_client_auth_session(raw: str) -> dict[str, Any]:
    text = unquote(str(raw or "").strip())
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_factor_id(*sources: Any) -> str:
    for src in sources:
        if src is None:
            continue
        if isinstance(src, str):
            m = FACTOR_URL_RE.search(src)
            if m:
                return m.group(1)
            # maybe raw id
            if re.fullmatch(r"[0-9a-fA-F]{16,64}", src.strip()):
                return src.strip()
            continue
        if isinstance(src, dict):
            # continue url fields
            for key in ("continue_url", "continueUrl", "url", "redirect_url"):
                found = extract_factor_id(src.get(key))
                if found:
                    return found
            page = src.get("page") if isinstance(src.get("page"), dict) else {}
            found = extract_factor_id(page.get("url") or page.get("continue_url"))
            if found:
                return found
            # nested factors
            for key in ("mfa_challenge_factors", "mfa_factors", "factors"):
                factors = src.get(key)
                if isinstance(factors, list):
                    for fac in factors:
                        if not isinstance(fac, dict):
                            continue
                        if str(fac.get("type") or fac.get("factor_type") or "").lower() in {
                            "totp",
                            "otp_totp",
                            "",
                        }:
                            fid = str(fac.get("id") or fac.get("factor_id") or "").strip()
                            if fid:
                                return fid
            # dig one level
            for v in src.values():
                if isinstance(v, (dict, list, str)):
                    found = extract_factor_id(v)
                    if found:
                        return found
        if isinstance(src, list):
            for item in src:
                found = extract_factor_id(item)
                if found:
                    return found
    return ""


def _decode_account_id_from_at(access_token: str) -> str:
    try:
        import base64

        parts = str(access_token or "").split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        auth = data.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or "").strip()
    except Exception:
        return ""


def issue_mfa_challenge(auth: AuthFlow, factor_id: str) -> dict[str, Any]:
    headers = auth._common_headers("https://auth.openai.com/log-in/password")
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "*/*"
    resp = auth.session.post(
        "https://auth.openai.com/api/accounts/mfa/issue_challenge",
        headers=headers,
        json={
            "id": factor_id,
            "type": "totp",
            "force_fresh_challenge": False,
        },
        timeout=30,
    )
    if hasattr(auth, "_trace_http"):
        try:
            auth._trace_http("mfa_issue_challenge", resp)
        except Exception:
            pass
    if resp.status_code != 200:
        raise MfaLoginError("MFA_FAILED", f"issue_challenge HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    try:
        return resp.json()
    except Exception:
        return {}


def verify_mfa_totp(auth: AuthFlow, factor_id: str, code: str) -> dict[str, Any]:
    referer = f"https://auth.openai.com/mfa-challenge/{factor_id}"
    headers = auth._common_headers(referer)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json"
    resp = auth.session.post(
        "https://auth.openai.com/api/accounts/mfa/verify",
        headers=headers,
        json={"id": factor_id, "type": "totp", "code": str(code).strip()},
        timeout=30,
    )
    if hasattr(auth, "_trace_http"):
        try:
            auth._trace_http("mfa_verify", resp)
        except Exception:
            pass
    if resp.status_code != 200:
        raise MfaLoginError("MFA_FAILED", f"verify HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    try:
        return resp.json()
    except Exception:
        return {}


def _finish_to_session(auth: AuthFlow, continue_url: str) -> AuthResult:
    continue_url = auth._normalize_continue_url(continue_url or "")
    if not continue_url:
        # last resort: try client auth dump / reauthorize helpers if available
        raise MfaLoginError("LOGIN_FAILED", "MFA 后缺少 continue_url/callback")
    callback_url, final_url = auth.follow_redirect_chain(continue_url)
    target = callback_url or continue_url
    try:
        if target and hasattr(auth, "oauth_token_exchange"):
            auth.oauth_token_exchange(callback_url or "", continue_url or "")
    except Exception:
        pass
    auth.get_auth_session()
    if not auth.result.is_valid():
        # sometimes get_auth_session fills only one side; try again after tiny wait
        time.sleep(0.5)
        auth.get_auth_session()
    if not auth.result.access_token and not auth.result.session_token:
        raise MfaLoginError(
            "LOGIN_FAILED",
            f"未能拿到 session/AT (final={_mask(final_url or '')})",
        )
    return auth.result


def login_with_password_and_totp(
    email: str,
    password: str,
    totp_secret: str,
    *,
    proxy: str | None = None,
) -> LoginSession:
    """账密 + TOTP 纯协议登录，返回可用 session/AT。"""
    email = (email or "").strip()
    password = (password or "").strip()
    totp_secret = (totp_secret or "").strip().replace(" ", "").upper()
    if not email or not password or not totp_secret:
        raise MfaLoginError("LOGIN_FAILED", "email/password/totp_secret 不能为空")

    cfg = Config(proxy=proxy or None)
    auth = AuthFlow(cfg)
    result = auth.result
    result.email = email
    result.password = password
    sess = LoginSession(
        email=email,
        password=password,
        totp_secret=totp_secret,
        auth=auth,
        result=result,
    )

    # 1) bootstrap
    csrf = auth.get_csrf_token()
    auth_url = auth.get_auth_url(csrf, email=email)
    device_id = auth.auth_oauth_init(auth_url)
    sentinel = auth.get_sentinel_token(device_id)
    sess.trace.append({"step": "bootstrap", "device_id": device_id})

    # 2) authorize continue login
    login_step = auth.authorize_continue(
        email=email,
        sentinel_token=sentinel,
        screen_hint="login",
        referer="https://auth.openai.com/log-in",
        trace_step="authorize_continue_login_rebind",
    )
    page_type = (auth._extract_page_type(login_step) or "").lower()
    continue_url = auth._normalize_continue_url(auth._extract_continue_url_from_step(login_step))
    sess.trace.append({"step": "authorize_continue", "page_type": page_type, "continue_url": continue_url[:180]})

    if page_type != "login_password" and "/log-in/password" not in (continue_url or ""):
        raise MfaLoginError(
            "LOGIN_FAILED",
            f"未进入密码登录页 page_type={page_type or '(empty)'} continue={continue_url[:120]}",
        )

    # 3) password verify（内部会带 sentinel）
    # 刷新一枚较新的 sentinel，贴近抓包
    try:
        auth.get_sentinel_token(device_id)
    except Exception:
        pass
    try:
        pwd_resp = auth.login_password_verify(password)
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if "invalid_username_or_password" in low or "401" in low:
            raise MfaLoginError("LOGIN_FAILED", f"用户名或密码错误: {msg[:240]}") from exc
        raise MfaLoginError("LOGIN_FAILED", f"password/verify 失败: {msg[:240]}") from exc
    page_type = (auth._extract_page_type(pwd_resp) or "").lower()
    continue_url = auth._normalize_continue_url(auth._extract_continue_url_from_step(pwd_resp))
    sess.trace.append(
        {
            "step": "password_verify",
            "page_type": page_type,
            "continue_url": (continue_url or "")[:180],
            "resp_keys": sorted(list(pwd_resp.keys()))[:20] if isinstance(pwd_resp, dict) else [],
        }
    )

    # 4) resolve factor id
    client_auth = _parse_client_auth_session(_cookie_value(auth.session, "oai-client-auth-session"))
    factor_id = extract_factor_id(continue_url, pwd_resp, client_auth)
    if not factor_id:
        # some responses put factors under page.payload
        factor_id = extract_factor_id((pwd_resp or {}).get("page") if isinstance(pwd_resp, dict) else None)
    if not factor_id:
        raise MfaLoginError("MFA_FAILED", "password/verify 后未解析到 totp factor_id")
    sess.factor_id = factor_id

    # 5) issue + verify totp
    issue_mfa_challenge(auth, factor_id)
    last_err = ""
    verify_resp: dict[str, Any] = {}
    ok = False
    for window in (1, 2):
        for code in totp_code_candidates(totp_secret, window=window):
            try:
                verify_resp = verify_mfa_totp(auth, factor_id, code)
                ok = True
                sess.trace.append({"step": "mfa_verify", "window": window, "code_masked": f"***{code[-2:]}"})
                break
            except MfaLoginError as exc:
                last_err = str(exc)
                continue
        if ok:
            break
    if not ok:
        raise MfaLoginError("MFA_FAILED", f"TOTP 校验失败: {last_err}")

    continue_url = auth._normalize_continue_url(
        auth._extract_continue_url_from_step(verify_resp) or continue_url
    )
    if not continue_url and isinstance(verify_resp, dict):
        for key in ("continue_url", "continueUrl", "redirect_url", "url"):
            if verify_resp.get(key):
                continue_url = auth._normalize_continue_url(str(verify_resp.get(key)))
                if continue_url:
                    break

    # if still empty, attempt to read continue from client auth cookie after verify
    if not continue_url:
        client_auth2 = _parse_client_auth_session(_cookie_value(auth.session, "oai-client-auth-session"))
        for key in ("continue_url", "continueUrl", "redirect_url"):
            if client_auth2.get(key):
                continue_url = auth._normalize_continue_url(str(client_auth2.get(key)))
                if continue_url:
                    break

    # 6) finish session
    if not continue_url:
        try:
            continue_url = auth._normalize_continue_url(auth._reauthorize_for_session(auth_url) or "")
        except Exception:
            continue_url = ""
    try:
        if continue_url:
            _finish_to_session(auth, continue_url)
        else:
            auth.get_auth_session()
    except Exception:
        try:
            auth.get_auth_session()
        except Exception:
            pass
    if not auth.result.access_token:
        # 最后兜底：若有 callback 形态 URL 可消费
        try:
            if continue_url and "code=" in continue_url and hasattr(auth, "_consume_callback_for_session"):
                auth._consume_callback_for_session(continue_url)
                auth.get_auth_session()
        except Exception:
            pass
    if not auth.result.access_token:
        raise MfaLoginError("LOGIN_FAILED", "登录完成但缺少 access_token")

    sess.account_id = _decode_account_id_from_at(auth.result.access_token)
    sess.trace.append(
        {
            "step": "session",
            "email": email,
            "has_at": bool(auth.result.access_token),
            "has_session": bool(auth.result.session_token),
            "account_id": sess.account_id,
            "at_masked": _mask(auth.result.access_token, 12, 6),
        }
    )
    return sess
