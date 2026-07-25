# -*- coding: utf-8 -*-
"""免手机验证 Codex auth 转化（学习自 zhishile/codex-auth-helper 的原理）。

原理：
- 注册拿到的 ChatGPT 网页 session accessToken 本身就是可调后端的凭证；
- Codex CLI / sub2api / Cockpit 消费的 auth.json 里，id_token 只被解析 claims，
  不做签名校验，因此可以本地构造 alg=none 的 synthetic id_token；
- 真正的 OAuth refresh_token 必须走 auth.openai.com（需要手机验证），
  这里用 sessionToken 占位；access token 过期后无法自动刷新，需重新转化。
  这是 codex-auth-helper 作者明示的限制，不是 bug。

风控说明：本模块默认纯本地转化（零网络请求）。仅当 access_token claims
缺少 chatgpt_account_id 时，调用方可以选择补一次 /api/auth/session。
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_EXPORT_DIR = _ROOT / "exports"

# refresh_token 占位值：与 codex-auth-helper 保持一致。
REFRESH_TOKEN_PLACEHOLDER = "placeholder"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """本地解码 JWT payload（不验签）。失败返回 {}。"""
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def build_synthetic_id_token(
    *,
    account_id: str,
    plan_type: str = "free",
    user_id: str = "",
    email: str = "",
    exp: int | None = None,
    iat: int | None = None,
) -> str:
    """构造 alg=none 的 synthetic id_token（codex-auth-helper 同款结构）。"""
    now = int(time.time())
    header = {"alg": "none", "typ": "JWT", "cpa_synthetic": True}
    payload: dict[str, Any] = {
        "iat": int(iat or now),
        "exp": int(exp or (now + 28 * 24 * 3600)),
        "https://api.openai.com/auth": {
            "chatgpt_account_id": str(account_id or ""),
            "chatgpt_plan_type": str(plan_type or "free"),
            "chatgpt_user_id": str(user_id or ""),
            "user_id": str(user_id or ""),
        },
    }
    if email:
        payload["email"] = str(email)
        payload["email_verified"] = True
    head = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    body = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return f"{head}.{body}.synthetic"


def build_chatgpt_auth_json(
    *,
    access_token: str,
    session_token: str = "",
    account_id: str,
    plan_type: str = "free",
    user_id: str = "",
    email: str = "",
    exp: int | None = None,
) -> dict[str, Any]:
    """组装 Codex 原生 auth.json（auth_mode=chatgpt）。"""
    id_token = build_synthetic_id_token(
        account_id=account_id,
        plan_type=plan_type,
        user_id=user_id,
        email=email,
        exp=exp,
    )
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": str(access_token or ""),
            "refresh_token": str(session_token or "") or REFRESH_TOKEN_PLACEHOLDER,
            "account_id": str(account_id or ""),
        },
        "last_refresh": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _row_extra(row: dict) -> dict:
    extra = row.get("extra_json") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    return extra if isinstance(extra, dict) else {}


def convert_account_row(row: dict) -> dict[str, Any]:
    """把一条账号记录本地转化为 synthetic Codex auth.json。

    返回 {"auth_json", "email", "account_id", "plan_type", "user_id",
          "expires_at", "session_token", "local_only"}；缺凭证时抛 ValueError。
    """
    extra = _row_extra(row)
    artifacts = extra.get("auth_artifacts") if isinstance(extra.get("auth_artifacts"), dict) else {}
    access_token = str(row.get("access_token") or artifacts.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("账号缺少 access_token，无法转化")

    claims = decode_jwt_claims(access_token)
    auth_claim = claims.get("https://api.openai.com/auth")
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}

    session = artifacts.get("session") if isinstance(artifacts.get("session"), dict) else {}
    session_account = session.get("account") if isinstance(session.get("account"), dict) else {}
    session_user = session.get("user") if isinstance(session.get("user"), dict) else {}

    account_id = str(
        auth_claim.get("chatgpt_account_id")
        or session_account.get("id")
        or row.get("account_id")
        or ""
    ).strip()
    if not account_id:
        raise ValueError("access_token claims 缺少 chatgpt_account_id，且本地无 session 数据可补")

    plan_type = str(
        auth_claim.get("chatgpt_plan_type")
        or session_account.get("planType")
        or row.get("plan_type")
        or "free"
    ).strip() or "free"
    user_id = str(
        auth_claim.get("chatgpt_user_id")
        or auth_claim.get("user_id")
        or session_user.get("id")
        or row.get("user_id")
        or ""
    ).strip()
    email = str(row.get("email") or claims.get("email") or session_user.get("email") or "").strip()
    exp_raw = claims.get("exp")
    try:
        exp = int(exp_raw) if exp_raw is not None else None
    except (TypeError, ValueError):
        exp = None
    session_token = str(
        session.get("sessionToken")
        or (session.get("session") or {}).get("sessionToken")
        or artifacts.get("session_token")
        or ""
    ).strip()

    auth_json = build_chatgpt_auth_json(
        access_token=access_token,
        session_token=session_token,
        account_id=account_id,
        plan_type=plan_type,
        user_id=user_id,
        email=email,
        exp=exp,
    )
    return {
        "auth_json": auth_json,
        "email": email,
        "account_id": account_id,
        "plan_type": plan_type,
        "user_id": user_id,
        "expires_at": exp,
        "session_token": session_token,
        "local_only": True,
    }


def build_sub2api_session_entry(converted: dict[str, Any], *, proxy_key: str | None = None) -> dict[str, Any]:
    """把转化结果转成 sub2api accounts[] 条目（oauth/session 型，可直接导入）。"""
    tokens = converted["auth_json"]["tokens"]
    entry = {
        "name": converted.get("email") or f"account-{str(converted.get('account_id'))[:8]}",
        "platform": "openai",
        "type": "oauth",
        "expires_at": converted.get("expires_at"),
        "auto_pause_on_expired": True,
        "concurrency": 10,
        "priority": 1,
        "credentials": {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
        },
        "extra": {
            "email": converted.get("email") or "",
            "account_id": converted.get("account_id") or "",
            "plan_type": converted.get("plan_type") or "free",
            "synthetic_id_token": True,
            "refreshable": bool(converted.get("session_token")),
            "source": "synthetic_auth",
            "note": "synthetic id_token（alg=none）；refresh_token 为 sessionToken 占位，过期后需重新转化",
        },
    }
    if proxy_key:
        entry["proxy_key"] = str(proxy_key)
    return entry


def build_cockpit_entry(converted: dict[str, Any]) -> dict[str, Any]:
    """把转化结果转成 Cockpit Tools 可导入的扁平 codex JSON。"""
    tokens = converted["auth_json"]["tokens"]
    return {
        "type": "codex",
        "id_token": tokens["id_token"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "account_id": converted.get("account_id") or "",
        "email": converted.get("email") or "",
        "expired": converted.get("expires_at"),
        "expires_at": converted.get("expires_at"),
        "synthetic_id_token": True,
        "refreshable": bool(converted.get("session_token")),
        "snapshot_exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _safe_stem(email: str) -> str:
    stem = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in str(email or ""))
    return stem[:64] or "account"


def write_auth_file(converted: dict[str, Any], *, export_dir: str | Path | None = None) -> Path:
    """写单个账号的 Codex auth.json 到 exports/，返回路径。"""
    out_dir = Path(export_dir or _EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = out_dir / f"synthetic-auth-{_safe_stem(converted.get('email'))}-{ts}.json"
    path.write_text(json.dumps(converted["auth_json"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        import os
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def write_sub2api_file(entries: list[dict], *, export_dir: str | Path | None = None) -> Path:
    out_dir = Path(export_dir or _EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = out_dir / f"synthetic-sub2api-{ts}.json"
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source": "synthetic_auth",
        "proxies": [],
        "accounts": entries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_cockpit_file(entries: list[dict], *, export_dir: str | Path | None = None) -> Path:
    out_dir = Path(export_dir or _EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = out_dir / f"synthetic-cockpit-{ts}.json"
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def synthetic_enabled() -> bool:
    from config import codex as codex_cfg
    return bool(getattr(codex_cfg, "CODEX_SYNTHETIC_AUTH_ENABLE", False))


def upload_to_sub2api(converted: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """把 synthetic auth.json 通过 sub2api 原生 codex-session 导入接口上传。

    复用 config.sub2api 的基址与鉴权；未配置基址时抛 ValueError。
    """
    import requests
    from config import sub2api as sub2api_cfg

    api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
    if api_base:
        url = f"{api_base.rstrip('/')}/api/v1/admin/accounts/import/codex-session"
    else:
        url = str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()
    if not url:
        raise ValueError("SUB2API_API_BASE 为空，无法上传到 sub2api")

    email = str(converted.get("email") or "").strip()
    payload = {
        "contents": [json.dumps(converted["auth_json"], ensure_ascii=False)],
        "name": email or f"account-{str(converted.get('account_id'))[:8]}",
        "update_existing": True,
        "concurrency": 3,
        "priority": 50,
        "confirm_mixed_channel_risk": True,
    }
    token = str(
        getattr(sub2api_cfg, "SUB2API_API_KEY", "")
        or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "")
        or ""
    ).strip()
    header_name = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
    prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "turb-gpt-free-register/synthetic-auth"}
    if token:
        headers[header_name] = f"{prefix} {token}".strip() if prefix else token

    wait = float(timeout or getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)
    resp = requests.post(url, headers=headers, json=payload, timeout=wait)
    status = int(getattr(resp, "status_code", 0) or 0)
    text = getattr(resp, "text", "") or ""
    if status < 200 or status >= 300:
        raise RuntimeError(f"sub2api 上传失败 HTTP {status}: {text[:400]}")
    try:
        body = resp.json()
    except Exception:
        body = {"text": text[:400]}
    return {"ok": True, "url": url, "status_code": status, "email": email, "response": body}
