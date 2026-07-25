# -*- coding: utf-8 -*-
"""
通用 API 取码邮箱客户端。

邮箱池导入格式：
    email----code_url

注册时领取 email；取码时直接 GET code_url，并从响应中提取 6 位验证码。
响应可以是纯文本、HTML 或 JSON，只要其中包含 6 位验证码即可。
"""
import json
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse
from dataclasses import dataclass
from pathlib import Path

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp
from core.otp_wait_policy import OTPProbeResult, resolve_wait_timeout, wait_for_otp_with_policy

logger = logging.getLogger(__name__)

_CODE_REGEX = re.compile(r"\b(\d{6})\b")
_CONTEXT_WORDS = ("code", "verify", "verification", "验证码", "代码", "确认码", "認証", "コード")
_CONTEXT_CACHE: dict[str, "GenericApiEmailAccount"] = {}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的API邮箱.txt"


class GenericApiMailError(RuntimeError):
    """通用 API 取码邮箱错误。"""


@dataclass
class GenericApiEmailAccount:
    email: str
    code_url: str


def validate_code_url(email: str, code_url: str) -> str:
    """保留原 URL；Assurivo open.php 的 mail 参数必须和素材邮箱一致。"""
    url = str(code_url or "").strip(); parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc: raise GenericApiMailError("取码地址必须是完整 http(s) URL")
    if is_assurivo_open_url(url):
        mail = unquote((parse_qs(parsed.query).get("mail") or [""])[0]).strip().lower()
        if mail and mail != str(email).strip().lower(): raise GenericApiMailError("Assurivo 查询地址的 mail 参数与邮箱不一致")
    return url


def is_assurivo_open_url(code_url: str) -> bool:
    """仅识别 Assurivo 的查询路由，不记录或重写其中的凭证参数。"""
    parsed = urlparse(str(code_url or "").strip())
    return bool(
        parsed.hostname
        and parsed.hostname.lower() == "assurivo.com"
        and parsed.path.rstrip("/").lower() == "/console/open.php"
    )


@dataclass
class GenericApiProbeState:
    best_otp: str | None = None
    ready_at_monotonic: float | None = None


def _flatten_json(obj) -> str:
    parts: list[str] = []
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            parts.append(str(x))
    walk(obj)
    return "\n".join(parts)


def _extract_code(text: str) -> str | None:
    """从纯文本/HTML/JSON 文本中提取 6 位 OTP。"""
    if not text:
        return None

    # 兼容 JSON：优先把所有 value 拉平再抽取。
    candidates_text = [text]
    try:
        parsed = json.loads(text)
        candidates_text.insert(0, _flatten_json(parsed))
    except Exception:
        pass

    for body in candidates_text:
        # 复用邮件 OTP 抽取逻辑。
        code = extract_otp({"text": body, "content": body, "subject": body[:200]})
        if code:
            return code

        codes = _CODE_REGEX.findall(body)
        if not codes:
            continue
        lower = body.lower()
        for code in codes:
            idx = lower.find(code)
            window = lower[max(0, idx - 80): idx + 86]
            if any(w.lower() in window for w in _CONTEXT_WORDS):
                return code
        return codes[-1]
    return None


def pick_account() -> GenericApiEmailAccount:
    """领取一个可用通用 API 邮箱。"""
    from core.assurivo_mail_client import migrate_legacy_generic_api_records
    from core.db import claim_next_generic_api_email, generic_api_email_pool_summary

    migrate_legacy_generic_api_records()
    inserted, skipped = import_from_file()
    if inserted:
        logger.info(f"[GenericAPI] 已自动从 {_ACCOUNTS_FILE.name} 导入 {inserted} 个邮箱（跳过 {skipped} 个）")

    row = claim_next_generic_api_email()
    if row is None:
        summary = generic_api_email_pool_summary()
        raise GenericApiMailError(
            f"通用 API 邮箱池没有可用账号: {summary}. 请在 WebUI 邮箱池导入：邮箱----取码地址"
        )
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[GenericAPI] 选中邮箱: {account.email}（DB id={row.get('id')}）")
    return account


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """从文本文件导入通用 API 邮箱，每行：email----code_url 或 email====code_url。"""
    from core.db import import_generic_api_emails
    p = Path(path) if path else _ACCOUNTS_FILE
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    if not p.exists():
        return 0, 0
    records = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----") if "----" in line else line.split("====")
        parts = [x.strip() for x in parts]
        if len(parts) < 2:
            continue
        records.append({"email": parts[0], "code_url": validate_code_url(parts[0], parts[1])})
    return import_generic_api_emails(records)


def get_account_context(email: str) -> GenericApiEmailAccount | None:
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    from core.db import get_generic_api_email_by_email
    row = get_generic_api_email_by_email(email)
    if row is None:
        return None
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[email] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email
    release_generic_api_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)


def fetch_otp_once(email: str, after_ts: float | None, state: GenericApiProbeState, settle_seconds: int | None = None) -> OTPProbeResult:
    """单次取码 probe；不 sleep、不维护总 deadline。"""
    account = get_account_context(email)
    if account is None: raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")
    resp = requests.get(account.code_url, headers={"Accept":"application/json,text/plain,*/*","User-Agent":"Mozilla/5.0 (compatible; gpt-register/1.0)"}, timeout=20, verify=False)
    if resp.status_code != 200: raise GenericApiMailError(f"通用 API HTTP {resp.status_code}")
    parsed = urlparse(account.code_url); code = None
    if is_assurivo_open_url(account.code_url):
        try: payload = resp.json()
        except ValueError: payload = {"html": resp.text or ""}
        from core.assurivo_mail_client import extract_new_openai_otp
        code = extract_new_openai_otp(payload, float(after_ts or 0))
    else: code = _extract_code(resp.text or "")
    now = time.monotonic(); settle = _email_cfg.OTP_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    if code and code != state.best_otp:
        state.best_otp, state.ready_at_monotonic = code, now + max(0, settle)
    if state.best_otp and state.ready_at_monotonic is not None and now >= state.ready_at_monotonic:
        return OTPProbeResult.completed(state.best_otp, state)
    return OTPProbeResult.candidate(state, state.ready_at_monotonic) if state.best_otp else OTPProbeResult.pending(state)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    state = GenericApiProbeState()
    timeout = resolve_wait_timeout(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_WAIT_TIMEOUT", None), _email_cfg.OTP_MAX_WAIT)
    return wait_for_otp_with_policy(provider="generic_api", email=email, initial_after_ts=after_ts or time.time(), fetch=lambda stamp: fetch_otp_once(email, stamp, state, settle_seconds), retrigger=lambda: time.time(), wait_timeout=timeout, poll_interval=poll_interval or _email_cfg.OTP_POLL_INTERVAL, retry_count=0)
