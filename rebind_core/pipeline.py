from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .change_email import ChangeEmailClient, ChangeEmailError, password_reauth
from .mail_inbox import wait_code
from .mfa_login import LoginSession, MfaLoginError, login_with_password_and_totp
from .paths import ROOT
from .session_export import build_login_bundle, write_login_bundle


class RebindError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class RebindResult:
    ok: bool
    code: str = "OK"
    message: str = ""
    old_email: str = ""
    new_email: str = ""
    bundle_path: str = ""
    access_token_masked: str = ""
    session_email: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    run_dir: str = ""


def _mask(value: str, head: int = 12, tail: int = 6) -> str:
    text = str(value or "")
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}...{text[-tail:]}"


def _log(trace: list[dict[str, Any]], step: str, **payload: Any) -> None:
    item = {"time": datetime.now().isoformat(timespec="seconds"), "step": step, **payload}
    # scrub
    for k in list(item.keys()):
        lk = k.lower()
        if any(x in lk for x in ("password", "totp", "secret", "cookie", "token")) and k not in {
            "access_token_masked",
            "has_at",
            "has_session",
        }:
            if isinstance(item[k], str) and len(item[k]) > 12:
                item[k] = _mask(item[k])
    trace.append(item)
    print(f"[{step}] " + ", ".join(f"{k}={v}" for k, v in item.items() if k not in {"time", "step"}))


def run_rebind_email(
    *,
    old_email: str,
    password: str,
    totp_secret: str,
    new_email: str,
    mail_api: str,
    proxy: str | None = None,
    out_dir: str | Path | None = None,
    mail_timeout: float = 120.0,
    otp_fetcher: Callable[[float | None, float], str] | None = None,
) -> RebindResult:
    old_email = (old_email or "").strip()
    new_email = (new_email or "").strip()
    password = (password or "").strip()
    totp_secret = (totp_secret or "").strip()
    mail_api = (mail_api or "").strip()
    trace: list[dict[str, Any]] = []
    run_dir = ROOT / "outputs" / "rebind_runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    def _fail(code: str, message: str) -> RebindResult:
        _log(trace, "failed", code=code, message=message)
        (run_dir / "trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return RebindResult(
            ok=False,
            code=code,
            message=message,
            old_email=old_email,
            new_email=new_email,
            trace=trace,
            run_dir=str(run_dir),
        )

    try:
        print("[1/6] 旧邮箱账密+TOTP 登录 ...")
        login1 = login_with_password_and_totp(
            old_email, password, totp_secret, proxy=proxy
        )
        _log(
            trace,
            "login_old",
            email=old_email,
            account_id=login1.account_id,
            at=_mask(login1.access_token),
            factor_id=login1.factor_id,
        )

        print("[2/6] 检查 change_email eligibility ...")
        client = ChangeEmailClient(login=login1)
        try:
            elig = client.eligibility()
        except ChangeEmailError as exc:
            return _fail(exc.code, exc.message)
        _log(trace, "eligibility", **{k: elig.get(k) for k in ("eligible", "eligibility_type")})

        print("[3/6] begin 发送新邮箱验证码 ...")
        issued_after = time.time()
        try:
            begin_resp = client.begin(new_email)
        except ChangeEmailError as exc:
            if exc.code == "REAUTH_FAILED":
                _log(trace, "begin_need_reauth", message=exc.message)
                print("begin 要求 reauth，执行 password+MFA 再试 ...")
                try:
                    password_reauth(login1)
                    # refresh client tokens
                    client = ChangeEmailClient(login=login1, session_id=str(uuid.uuid4()))
                    begin_resp = client.begin(new_email)
                except Exception as exc2:
                    return _fail("REAUTH_FAILED", str(exc2))
            else:
                return _fail(exc.code, exc.message)
        _log(trace, "begin", new_email=new_email, resp_keys=list(begin_resp.keys())[:10] if isinstance(begin_resp, dict) else [])

        print("[4/6] 等待新邮箱验证码 ...")
        try:
            if otp_fetcher is not None:
                code = otp_fetcher(issued_after - 5, float(mail_timeout))
            else:
                code = wait_code(mail_api, issued_after=issued_after - 5, timeout=mail_timeout)
        except TimeoutError as exc:
            return _fail("MAIL_TIMEOUT", str(exc))
        _log(trace, "mail_code", code_tail=code[-2:])

        print("[5/6] verify 换绑 ...")
        try:
            verify_resp = client.verify(new_email, code)
        except ChangeEmailError as exc:
            return _fail(exc.code, exc.message)
        _log(trace, "verify", resp_keys=list(verify_resp.keys())[:10] if isinstance(verify_resp, dict) else [])

        print("[6/6] 新邮箱账密+TOTP 重登并导出 ...")
        # 主动重建登录会话
        try:
            login2 = login_with_password_and_totp(
                new_email, password, totp_secret, proxy=proxy
            )
        except MfaLoginError as exc:
            return _fail(exc.code if exc.code in {"LOGIN_FAILED", "MFA_FAILED"} else "RELOGIN_FAILED", exc.message)

        bundle = build_login_bundle(login2, rebind_email=new_email)
        session_email = str(bundle.get("email") or "")
        if session_email and session_email.lower() != new_email.lower():
            return _fail(
                "RELOGIN_FAILED",
                f"重登后 email 不匹配: got={session_email} expected={new_email}",
            )
        paths = write_login_bundle(bundle, out_dir=out_dir)
        _log(
            trace,
            "export",
            bundle=str(paths["bundle"]),
            email=session_email,
            at=_mask(login2.access_token),
        )
        (run_dir / "trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "login_bundle.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return RebindResult(
            ok=True,
            code="OK",
            message="rebind success",
            old_email=old_email,
            new_email=new_email,
            bundle_path=str(paths["bundle"]),
            access_token_masked=_mask(login2.access_token),
            session_email=session_email,
            trace=trace,
            run_dir=str(run_dir),
        )
    except MfaLoginError as exc:
        return _fail(exc.code, exc.message)
    except ChangeEmailError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:
        return _fail("EXPORT_FAILED", str(exc))
