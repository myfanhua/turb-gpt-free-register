#!/usr/bin/env python3
"""ChatGPT TOTP 2FA 独立协议（与注册主流程解耦）。

来源抓包：
  - outputs/captures/password_2fa_20260805_075534
  - outputs/captures/manual_login_20260805_074138
  - outputs/session_exports/*_2fa.json
  - outputs/session_exports/mfa_protocol_partial.json

完整链路（Bearer AT + chatgpt session cookie）：
  1) GET  /backend-api/accounts/mfa_info
  2) POST /backend-api/accounts/mfa/enroll
       -> { secret, session_id, factor: { id, factor_type: "totp" } }
  3) 本地用 secret 算 6 位 TOTP
  4) POST /backend-api/accounts/mfa/user/activate_enrollment
       body: { "code": "123456", "factor_type": "totp", "session_id": "<enroll.session_id>" }
       -> { success: true }（或等价成功）
  5) GET  /backend-api/accounts/mfa_info
       -> mfa_enabled=true, factors.totp 含 factor_id

注意：
  - passwordless 账号通常无“设置密码”入口（add_password_eligibility=false）
  - 需要 recent auth；纯 cookie 注入有时会 recent_auth_required
  - 本模块独立，不挂注册主路径
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import quote

try:
    from registration_core.http_client import create_http_session
except Exception:  # pragma: no cover
    create_http_session = None  # type: ignore
try:
    from registration_core.traffic_meter import MeteredSession
except Exception:  # pragma: no cover
    MeteredSession = None  # type: ignore

MFA_INFO_URL = "https://chatgpt.com/backend-api/accounts/mfa_info"
MFA_ENROLL_URL = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
MFA_ACTIVATE_URL = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"


@dataclass
class MfaEnrollResult:
    secret: str
    session_id: str
    factor_id: str
    factor_type: str = "totp"
    raw: dict[str, Any] = field(default_factory=dict)

    def otpauth_uri(self, account_name: str = "chatgpt", issuer: str = "OpenAI") -> str:
        label = quote(f"{issuer}:{account_name}")
        return (
            f"otpauth://totp/{label}?secret={self.secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
        )


@dataclass
class MfaProtocolResult:
    ok: bool
    mfa_enabled: bool = False
    secret: str = ""
    factor_id: str = ""
    enroll_session_id: str = ""
    factor_type: str = "totp"
    otpauth_uri: str = ""
    activate_raw: dict[str, Any] = field(default_factory=dict)
    info_before: dict[str, Any] = field(default_factory=dict)
    info_after: dict[str, Any] = field(default_factory=dict)
    traffic: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def totp_code(secret: str, for_time: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (SHA1). secret 为 Base32。"""
    key = _b32decode(secret)
    counter = int((time.time() if for_time is None else for_time) // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def totp_code_candidates(
    secret: str,
    *,
    for_time: float | None = None,
    step: int = 30,
    digits: int = 6,
    window: int = 1,
) -> list[str]:
    """Return unique TOTP codes for current and adjacent time windows.

    ``window=1`` yields t-30 / t / t+30 (up to 3 codes). Used to absorb small
    clock skew and boundary races on activate_enrollment.
    """

    now = time.time() if for_time is None else float(for_time)
    win = max(0, int(window))
    codes: list[str] = []
    seen: set[str] = set()
    # Prefer current window first, then nearest neighbors.
    offsets = [0]
    for delta in range(1, win + 1):
        offsets.extend((-delta, delta))
    for off in offsets:
        code = totp_code(secret, for_time=now + off * step, step=step, digits=digits)
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _is_invalid_code_error(exc: BaseException | str) -> bool:
    text = str(exc or "").lower()
    return ("invalid_code" in text) or ("invalid code" in text)


def _b32decode(secret: str) -> bytes:
    s = (secret or "").strip().replace(" ", "").upper()
    # Some enroll responses include spaces or lowercase; normalize strictly.
    s = "".join(ch for ch in s if ch.isalnum())
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad, casefold=True)


def _auth_headers(access_token: str, cookie_header: str = "", device_id: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Authorization": f"Bearer {(access_token or '').strip()}",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if device_id:
        headers["OAI-Device-Id"] = device_id
    return headers


class MfaTotpProtocol:
    """独立 2FA 协议客户端。"""

    def __init__(
        self,
        *,
        access_token: str,
        cookie_header: str = "",
        device_id: str = "",
        proxy: str = "",
        timeout: float = 30.0,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        if create_http_session is None:
            raise RuntimeError("http_client.create_http_session 不可用")
        self.access_token = (access_token or "").strip()
        if not self.access_token:
            raise ValueError("需要 access_token")
        self.cookie_header = (cookie_header or "").strip()
        self.device_id = (device_id or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = timeout
        self.log = logger or (lambda msg: None)
        raw_session = create_http_session(proxy=self.proxy or None)
        self.session = MeteredSession(raw_session, stage="totp") if MeteredSession is not None else raw_session
        self.headers = _auth_headers(self.access_token, self.cookie_header, self.device_id)

    def _traffic_snapshot(self) -> dict[str, Any]:
        snap = getattr(self.session, "snapshot", None)
        if callable(snap):
            try:
                data = snap()
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def close(self) -> None:
        try:
            if hasattr(self.session, "close"):
                self.session.close()
        except Exception:
            pass

    def mfa_info(self) -> dict[str, Any]:
        self.log("GET mfa_info")
        resp = self.session.get(
            MFA_INFO_URL,
            headers={
                **self.headers,
                "x-openai-target-path": "/backend-api/accounts/mfa_info",
                "x-openai-target-route": "/backend-api/accounts/mfa_info",
            },
            timeout=self.timeout,
        )
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            raise RuntimeError(f"mfa_info HTTP {resp.status_code}: {(resp.text or '')[:240]}")
        return resp.json() if hasattr(resp, "json") else json.loads(resp.text or "{}")

    def enroll(self) -> MfaEnrollResult:
        """POST mfa/enroll → secret + session_id + factor_id。"""
        self.log("POST mfa/enroll")
        resp = self.session.post(
            MFA_ENROLL_URL,
            headers={
                **self.headers,
                "x-openai-target-path": "/backend-api/accounts/mfa/enroll",
                "x-openai-target-route": "/backend-api/accounts/mfa/enroll",
            },
            # Live API currently requires factor_type in the JSON body.
            json={"factor_type": "totp"},
            timeout=self.timeout,
        )
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            raise RuntimeError(f"mfa/enroll HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        data = resp.json() if hasattr(resp, "json") else json.loads(resp.text or "{}")
        secret = str(data.get("secret") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        factor = data.get("factor") if isinstance(data.get("factor"), dict) else {}
        factor_id = str(factor.get("id") or "").strip()
        factor_type = str(factor.get("factor_type") or "totp").strip() or "totp"
        if not secret or not session_id or not factor_id:
            raise RuntimeError(f"mfa/enroll 响应缺字段: {json.dumps(data, ensure_ascii=False)[:300]}")
        self.log(f"enroll ok secret_len={len(secret)} factor_id={factor_id[:12]}…")
        return MfaEnrollResult(
            secret=secret,
            session_id=session_id,
            factor_id=factor_id,
            factor_type=factor_type,
            raw=data if isinstance(data, dict) else {},
        )

    def activate_enrollment(self, *, code: str, session_id: str, factor_type: str = "totp") -> dict[str, Any]:
        """POST activate_enrollment body={code, factor_type, session_id}。"""
        body = {
            "code": str(code).strip(),
            "factor_type": factor_type or "totp",
            "session_id": session_id,
        }
        self.log(f"POST activate_enrollment code={body['code']} session_prefix={str(session_id)[:8]}")
        resp = self.session.post(
            MFA_ACTIVATE_URL,
            headers={
                **self.headers,
                "x-openai-target-path": "/backend-api/accounts/mfa/user/activate_enrollment",
                "x-openai-target-route": "/backend-api/accounts/mfa/user/activate_enrollment",
            },
            json=body,
            timeout=self.timeout,
        )
        text = getattr(resp, "text", "") or ""
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            raise RuntimeError(f"activate_enrollment HTTP {resp.status_code}: {text[:300]}")
        try:
            data = resp.json() if hasattr(resp, "json") else json.loads(text or "{}")
        except Exception:
            data = {"raw": text[:500]}
        return data if isinstance(data, dict) else {"raw": data}

    def _activate_with_window_retries(
        self,
        *,
        secret: str,
        session_id: str,
        factor_type: str = "totp",
        window: int = 1,
    ) -> dict[str, Any]:
        """Try activate_enrollment across adjacent TOTP windows.

        On ``invalid_code`` keep trying remaining candidates. Other errors raise.
        """

        last_exc: Exception | None = None
        now = time.time()
        boundary = now % 30.0
        self.log(
            f"activate window retry: secret_len={len(secret or '')} "
            f"time_mod30={boundary:.2f}s window={window}"
        )
        for idx, code in enumerate(totp_code_candidates(secret, for_time=now, window=window)):
            try:
                raw = self.activate_enrollment(
                    code=code,
                    session_id=session_id,
                    factor_type=factor_type,
                )
                if idx:
                    self.log(f"activate succeeded on candidate#{idx + 1}")
                return raw
            except Exception as exc:
                last_exc = exc
                if _is_invalid_code_error(exc):
                    self.log(f"activate invalid_code on candidate#{idx + 1}: {exc}")
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def enable_totp(
        self,
        *,
        account_name: str = "chatgpt",
        max_enroll_attempts: int = 2,
        settle_seconds: float = 1.0,
        window: int = 1,
    ) -> MfaProtocolResult:
        """一键：info → enroll → totp(多窗口) → activate → info。

        - 注册刚完成后给服务端一点 settle 时间
        - invalid_code 时先换相邻时间窗，再重新 enroll 重试
        """
        attempts = max(1, int(max_enroll_attempts or 1))
        settle = max(0.0, float(settle_seconds or 0.0))
        win = max(0, int(window))
        try:
            if settle > 0:
                self.log(f"TOTP settle sleep {settle:.1f}s before mfa_info")
                time.sleep(settle)
            info_before = self.mfa_info()
            if bool(info_before.get("mfa_enabled")):
                totp_factors = []
                factors = info_before.get("factors") if isinstance(info_before.get("factors"), dict) else {}
                if isinstance(factors.get("totp"), list):
                    totp_factors = factors.get("totp") or []
                fid = ""
                if totp_factors and isinstance(totp_factors[0], dict):
                    fid = str(totp_factors[0].get("id") or "")
                return MfaProtocolResult(
                    ok=True,
                    mfa_enabled=True,
                    factor_id=fid,
                    info_before=info_before,
                    info_after=info_before,
                    traffic=self._traffic_snapshot(),
                    error="already_enabled",
                )

            last_error = ""
            last_activate: dict[str, Any] = {}
            last_enroll: MfaEnrollResult | None = None
            for attempt in range(1, attempts + 1):
                try:
                    enroll = self.enroll()
                    last_enroll = enroll
                    self.log(
                        f"enroll attempt={attempt}/{attempts} "
                        f"secret_len={len(enroll.secret)} factor={enroll.factor_id[:12]} "
                        f"session={enroll.session_id[:12]}"
                    )
                    activate_raw = self._activate_with_window_retries(
                        secret=enroll.secret,
                        session_id=enroll.session_id,
                        factor_type=enroll.factor_type,
                        window=win,
                    )
                    last_activate = activate_raw if isinstance(activate_raw, dict) else {}
                    info_after = self.mfa_info()
                    enabled = bool(info_after.get("mfa_enabled") or last_activate.get("success") is True)
                    if enabled:
                        return MfaProtocolResult(
                            ok=True,
                            mfa_enabled=True,
                            secret=enroll.secret,
                            factor_id=enroll.factor_id,
                            enroll_session_id=enroll.session_id,
                            factor_type=enroll.factor_type,
                            otpauth_uri=enroll.otpauth_uri(account_name=account_name),
                            activate_raw=last_activate,
                            info_before=info_before if isinstance(info_before, dict) else {},
                            info_after=info_after if isinstance(info_after, dict) else {},
                            traffic=self._traffic_snapshot(),
                            error="",
                        )
                    last_error = "activate_not_confirmed"
                    self.log(f"enroll attempt={attempt} activate not confirmed")
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    self.log(f"enroll attempt={attempt} failed: {last_error}")
                    if attempt >= attempts:
                        break
                    # Brief pause then re-enroll for a fresh secret/session_id.
                    time.sleep(0.6)
                    continue

            return MfaProtocolResult(
                ok=False,
                mfa_enabled=False,
                secret=str(getattr(last_enroll, "secret", "") or ""),
                factor_id=str(getattr(last_enroll, "factor_id", "") or ""),
                enroll_session_id=str(getattr(last_enroll, "session_id", "") or ""),
                factor_type=str(getattr(last_enroll, "factor_type", "") or "totp") or "totp",
                otpauth_uri=(
                    last_enroll.otpauth_uri(account_name=account_name) if last_enroll is not None else ""
                ),
                activate_raw=last_activate,
                info_before=info_before if isinstance(info_before, dict) else {},
                info_after={},
                traffic=self._traffic_snapshot(),
                error=last_error or "activate_not_confirmed",
            )
        except Exception as exc:
            return MfaProtocolResult(
                ok=False,
                traffic=self._traffic_snapshot(),
                error=f"{type(exc).__name__}: {exc}",
            )


def enable_totp_for_token(
    access_token: str,
    *,
    cookie_header: str = "",
    device_id: str = "",
    proxy: str = "",
    account_name: str = "chatgpt",
    max_enroll_attempts: int = 2,
    settle_seconds: float = 1.0,
    window: int = 1,
) -> dict[str, Any]:
    """便捷入口：给 AT 开 TOTP，返回 dict。"""
    client = MfaTotpProtocol(
        access_token=access_token,
        cookie_header=cookie_header,
        device_id=device_id,
        proxy=proxy,
    )
    try:
        return client.enable_totp(
            account_name=account_name,
            max_enroll_attempts=max_enroll_attempts,
            settle_seconds=settle_seconds,
            window=window,
        ).to_dict()
    finally:
        client.close()


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Standalone ChatGPT TOTP MFA protocol")
    parser.add_argument("--access-token", default="")
    parser.add_argument("--state", default="", help="login_state json path")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--email", default="chatgpt")
    args = parser.parse_args()

    at = args.access_token
    cookie = ""
    device = ""
    if args.state:
        data = json.loads(Path(args.state).read_text(encoding="utf-8"))
        at = at or str(data.get("access_token") or "")
        cookie = str(data.get("cookie_header") or "")
        st = str(data.get("session_token") or "")
        if not cookie and st and not st.startswith("{"):
            cookie = f"__Secure-next-auth.session-token={st}"
        device = str(data.get("device_id") or "")
        if not args.email or args.email == "chatgpt":
            args.email = str(data.get("email") or args.email)

    result = enable_totp_for_token(
        at,
        cookie_header=cookie,
        device_id=device,
        proxy=args.proxy,
        account_name=args.email,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
