from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .mfa_login import LoginSession, MfaLoginError

CHAT_ORIGIN = "https://chatgpt.com"
ELIGIBILITY_PATH = "/backend-api/accounts/change_email/eligibility"
BEGIN_PATH = "/backend-api/accounts/change_email/begin"
VERIFY_PATH = "/backend-api/accounts/change_email/verify"

# 来自本次成功抓包常量；若首页可解析再覆盖
DEFAULT_CLIENT_VERSION = "prod-46437587156517d920436051cb9ab60a95f0503a"
DEFAULT_CLIENT_BUILD = "9723596"


class ChangeEmailError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class ChangeEmailClient:
    login: LoginSession
    client_version: str = DEFAULT_CLIENT_VERSION
    client_build: str = DEFAULT_CLIENT_BUILD
    language: str = "zh-CN"
    session_id: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        if not self.login.device_id:
            # AuthFlow 通常已写入
            did = getattr(self.login.result, "device_id", "") or ""
            if not did:
                raise ChangeEmailError("LOGIN_FAILED", "缺少 oai-device-id")
        if not self.login.access_token:
            raise ChangeEmailError("LOGIN_FAILED", "缺少 access_token")
        if not self.login.account_id:
            # 允许稍后从 /me 补
            pass

    def _device_id(self) -> str:
        return str(self.login.device_id or self.login.result.device_id or "")

    def _headers(self, path: str, *, with_json: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.login.access_token}",
            "Accept": "*/*",
            "Referer": f"{CHAT_ORIGIN}/",
            "oai-device-id": self._device_id(),
            "oai-session-id": self.session_id,
            "oai-client-version": self.client_version,
            "oai-client-build-number": self.client_build,
            "oai-language": self.language,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
        if with_json:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = CHAT_ORIGIN
        account_id = str(self.login.account_id or "").strip()
        if account_id and path != ELIGIBILITY_PATH:
            headers["chatgpt-account-id"] = account_id
        # 带上 cookie（session）增强兼容
        cookie = self.login.cookie_header
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _session(self):
        return self.login.auth.session

    def ensure_account_id(self) -> str:
        if self.login.account_id:
            return self.login.account_id
        resp = self._session().get(
            f"{CHAT_ORIGIN}/backend-api/me",
            headers=self._headers("/backend-api/me"),
            timeout=30,
        )
        if resp.status_code != 200:
            # fallback: accounts/check
            return ""
        try:
            data = resp.json()
        except Exception:
            return ""
        # /me may not include account id; try orgs later
        # Prefer decode already done; keep empty if unknown
        orgs = data.get("orgs") if isinstance(data, dict) else None
        return self.login.account_id

    def eligibility(self) -> dict[str, Any]:
        resp = self._session().get(
            f"{CHAT_ORIGIN}{ELIGIBILITY_PATH}",
            headers=self._headers(ELIGIBILITY_PATH),
            timeout=30,
        )
        body = (resp.text or "")[:300]
        if resp.status_code != 200:
            raise ChangeEmailError("NOT_ELIGIBLE", f"eligibility HTTP {resp.status_code}: {body}")
        try:
            data = resp.json()
        except Exception as exc:
            raise ChangeEmailError("NOT_ELIGIBLE", f"eligibility 非 JSON: {exc}; body={body}") from exc
        eligible = bool(data.get("eligible") is True)
        etype = str(data.get("eligibility_type") or "")
        if not eligible:
            raise ChangeEmailError("NOT_ELIGIBLE", f"eligible=false type={etype} body={data}")
        if etype and etype != "password":
            # 仍允许继续，但标注
            pass
        return data if isinstance(data, dict) else {"eligible": True, "eligibility_type": etype}

    def begin(self, new_email: str, *, auth0_client_id: str = "", social_user: bool = False) -> dict[str, Any]:
        new_email = (new_email or "").strip()
        if not new_email or "@" not in new_email:
            raise ChangeEmailError("BEGIN_FAILED", "新邮箱非法")
        if not self.login.account_id:
            # begin 抓包带 chatgpt-account-id；尽量补齐
            self.ensure_account_id()
        payload: dict[str, Any] = {"email": new_email}
        if auth0_client_id:
            payload["auth0_client_id"] = auth0_client_id
        if social_user:
            payload["remove_social_subs"] = True
        resp = self._session().post(
            f"{CHAT_ORIGIN}{BEGIN_PATH}",
            headers=self._headers(BEGIN_PATH, with_json=True),
            json=payload,
            timeout=30,
        )
        body = (resp.text or "")[:400]
        if resp.status_code != 200:
            low = body.lower()
            if "recent" in low or "reauth" in low or resp.status_code in {401, 403}:
                raise ChangeEmailError("REAUTH_FAILED", f"begin 需要 reauth: HTTP {resp.status_code} {body}")
            raise ChangeEmailError("BEGIN_FAILED", f"begin HTTP {resp.status_code}: {body}")
        try:
            return resp.json() if resp.text else {"ok": True}
        except Exception:
            return {"ok": True, "raw": body}

    def verify(self, new_email: str, code: str, *, social_user: bool = False) -> dict[str, Any]:
        new_email = (new_email or "").strip()
        code = str(code or "").strip()
        if not (new_email and code):
            raise ChangeEmailError("VERIFY_FAILED", "email/code 不能为空")
        payload: dict[str, Any] = {"email": new_email, "code": code}
        if social_user:
            payload["remove_social_subs"] = True
        resp = self._session().post(
            f"{CHAT_ORIGIN}{VERIFY_PATH}",
            headers=self._headers(VERIFY_PATH, with_json=True),
            json=payload,
            timeout=30,
        )
        body = (resp.text or "")[:400]
        if resp.status_code != 200:
            raise ChangeEmailError("VERIFY_FAILED", f"verify HTTP {resp.status_code}: {body}")
        try:
            return resp.json() if resp.text else {"ok": True}
        except Exception:
            return {"ok": True, "raw": body}


def password_reauth(login: LoginSession) -> dict[str, Any]:
    """可选：换绑前再次 password/verify + MFA（当 begin 要求 recent auth）。"""
    auth = login.auth
    device_id = login.device_id or auth.result.device_id
    try:
        auth.get_sentinel_token(device_id)
    except Exception:
        pass
    pwd_resp = auth.login_password_verify(login.password)
    from .mfa_login import extract_factor_id, issue_mfa_challenge, verify_mfa_totp
    from registration_core.mfa_totp_protocol import totp_code_candidates

    factor_id = extract_factor_id(
        auth._extract_continue_url_from_step(pwd_resp),
        pwd_resp,
        login.factor_id,
    ) or login.factor_id
    if not factor_id:
        raise ChangeEmailError("REAUTH_FAILED", "reauth 后无 factor_id")
    issue_mfa_challenge(auth, factor_id)
    ok = False
    last = ""
    for code in totp_code_candidates(login.totp_secret, window=2):
        try:
            verify_mfa_totp(auth, factor_id, code)
            ok = True
            break
        except MfaLoginError as exc:
            last = str(exc)
    if not ok:
        raise ChangeEmailError("REAUTH_FAILED", f"reauth MFA 失败: {last}")
    # 刷新 AT/session
    try:
        auth.get_auth_session()
    except Exception:
        pass
    login.factor_id = factor_id
    return {"ok": True, "factor_id": factor_id}
