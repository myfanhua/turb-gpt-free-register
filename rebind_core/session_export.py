from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .mfa_login import LoginSession
from .paths import ROOT

DEFAULT_OUT = ROOT / "outputs" / "session_export"


def _safe_filename(value: str) -> str:
    import re

    text = re.sub(r"[^A-Za-z0-9._@-]+", "_", (value or "").strip())
    return text[:80] or "unknown"


def _parse_auth_session(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def split_session_cookie_chunks(token: str, chunk_size: int = 3500) -> list[str]:
    token = str(token or "")
    if not token:
        return []
    if len(token) <= chunk_size:
        return [token]
    return [token[i : i + chunk_size] for i in range(0, len(token), chunk_size)]


def build_login_bundle(
    login: LoginSession,
    *,
    rebind_email: str = "",
    login_method: str = "password+2fa",
) -> dict[str, Any]:
    result = login.result
    auth_session = _parse_auth_session(result.auth_session_json)
    email = ""
    user_id = ""
    expires = ""
    if isinstance(auth_session, dict):
        user = auth_session.get("user") if isinstance(auth_session.get("user"), dict) else {}
        email = str(user.get("email") or auth_session.get("email") or result.email or "")
        user_id = str(user.get("id") or "")
        expires = str(auth_session.get("expires") or "")
    email = email or result.email
    token = str(result.session_token or "")
    chunks = split_session_cookie_chunks(token)
    chunk_names = [f"__Secure-next-auth.session-token.{i}" for i in range(len(chunks))] if len(chunks) > 1 else None
    if len(chunks) > 1:
        header = "; ".join(f"{n}={v}" for n, v in zip(chunk_names, chunks))
    else:
        header = f"__Secure-next-auth.session-token={token}" if token else ""

    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "email": email,
        "user_id": user_id,
        "session_cookie_name": "__Secure-next-auth.session-token",
        "session_token": token,
        "session_token_chunks": list(range(len(chunks))) if len(chunks) > 1 else None,
        "access_token": str(result.access_token or ""),
        "expires": expires,
        "auth_session_http_status": 200 if auth_session else 0,
        "auth_session_ok": bool(auth_session),
        "rebind_email": rebind_email or email,
        "login_method": login_method,
        "device_id": str(result.device_id or ""),
        "account_id": str(login.account_id or ""),
        "cookie": {
            "name": "__Secure-next-auth.session-token",
            "value": token,
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "chunked": bool(chunk_names),
            "chunk_names": chunk_names,
        },
        "auth_session": auth_session,
        "inject_hint": {
            "cookie_header": header or str(result.cookie_header or ""),
            "domain": ".chatgpt.com",
            "url": "https://chatgpt.com/",
            "note": "分片 cookie 注入时需按 chunk_names 种回。",
        },
    }


def write_login_bundle(bundle: dict[str, Any], out_dir: Path | None = None) -> dict[str, Path]:
    out_dir = Path(out_dir or DEFAULT_OUT)
    out_dir.mkdir(parents=True, exist_ok=True)
    email = str(bundle.get("email") or "")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = _safe_filename(email.replace("@", "_at_")) if email else stamp

    paths = {
        "bundle": out_dir / f"login_bundle_{suffix}.json",
        "bundle_latest": out_dir / "login_bundle_latest.json",
        "session_cookie": out_dir / f"session_cookie_{suffix}.txt",
        "session_cookie_latest": out_dir / "session_cookie_latest.txt",
        "access_token": out_dir / f"access_token_{suffix}.txt",
        "access_token_latest": out_dir / "access_token_latest.txt",
        "auth_session": out_dir / f"auth_session_{suffix}.json",
        "cookie_header": out_dir / f"session_cookie_header_{suffix}.txt",
        "cookie_header_latest": out_dir / "session_cookie_header_latest.txt",
    }

    text = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    paths["bundle"].write_text(text, encoding="utf-8")
    paths["bundle_latest"].write_text(text, encoding="utf-8")

    token = str(bundle.get("session_token") or "")
    paths["session_cookie"].write_text(token + ("\n" if token else ""), encoding="utf-8")
    paths["session_cookie_latest"].write_text(token + ("\n" if token else ""), encoding="utf-8")

    at = str(bundle.get("access_token") or "")
    paths["access_token"].write_text(at + ("\n" if at else ""), encoding="utf-8")
    paths["access_token_latest"].write_text(at + ("\n" if at else ""), encoding="utf-8")

    auth = bundle.get("auth_session")
    paths["auth_session"].write_text(
        json.dumps(auth or {}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    header = str(((bundle.get("inject_hint") or {}) if isinstance(bundle.get("inject_hint"), dict) else {}).get("cookie_header") or "")
    paths["cookie_header"].write_text(header + ("\n" if header else ""), encoding="utf-8")
    paths["cookie_header_latest"].write_text(header + ("\n" if header else ""), encoding="utf-8")
    return paths
