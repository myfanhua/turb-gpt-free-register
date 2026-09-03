"""
Codex Agent Identity 自动注册脚本
by 久雾

流程：
1. 通过 ChatGPT session JWT 获取账号信息
2. 生成 Ed25519 密钥对
3. 在 auth.openai.com 注册 agent
4. 生成 Codex CLI 可用的 auth.json

依赖：curl_cffi, cryptography
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import string
import sys
import time
import uuid
from typing import Any

from curl_cffi import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
    load_pem_private_key,
)


# ============================================================
#  常量
# ============================================================

AUTHAPI_BASE = "https://auth.openai.com/api/accounts"
CHATGPT_BASE = "https://chatgpt.com"
IMPERSONATE = "chrome"

CHROME_VERSION = "146"
USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
)

# Codex CLI agent 版本信息
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"


# ============================================================
#  日志
# ============================================================

logger = logging.getLogger(__name__)


def _log(step: str, msg: str, level: str = "INFO") -> None:
    """统一走 logging，避免 WebUI/任务日志里混入 ANSI 彩色 stdout。"""
    text = f"[CodexAgent][{step}] {msg}"
    if level in {"WARN", "WARNING"}:
        logger.warning(text)
    elif level == "ERROR":
        logger.error(text)
    else:
        logger.info(text)


def _banner(title: str) -> None:
    logger.info("[CodexAgent] %s", title)


def _fingerprint(value: str, length: int = 12) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:length]


# ============================================================
#  Ed25519 密钥对生成
# ============================================================

def generate_ed25519_keypair() -> tuple[str, str]:
    """
    生成 Ed25519 密钥对。

    :return: (private_key_pkcs8_base64, public_key_ssh)
    """
    private_key = Ed25519PrivateKey.generate()

    # PKCS8 DER 格式私钥 → base64
    pkcs8_der = private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    private_key_b64 = base64.b64encode(pkcs8_der).decode()

    # 原始公钥字节
    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )

    # 构造 SSH 公钥格式: ssh-ed25519 base64(blob)
    ssh_header = b"ssh-ed25519"
    blob = bytearray()
    blob.extend(len(ssh_header).to_bytes(4, "big"))
    blob.extend(ssh_header)
    blob.extend(len(pub_bytes).to_bytes(4, "big"))
    blob.extend(pub_bytes)
    ssh_b64 = base64.b64encode(bytes(blob)).decode()
    public_key_ssh = f"ssh-ed25519 {ssh_b64}"

    return private_key_b64, public_key_ssh


# ============================================================
#  JWT 解码（不验证签名，仅提取 claims）
# ============================================================

def decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
    """
    解码 JWT payload（不验证签名）。

    :param jwt_token: JWT 字符串
    :return: claims dict
    """
    parts = jwt_token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")

    # JWT payload 是 base64url 编码
    payload_b64 = parts[1]
    # 补齐 padding
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding

    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


# ============================================================
#  Session 获取
# ============================================================

def get_session_from_cookies(cookies: dict[str, str]) -> dict[str, Any]:
    """
    使用 cookies 调用 /api/auth/session 获取 accessToken 和账号信息。

    :param cookies: chatgpt.com 的 cookies dict
    :return: session 数据
    """
    r = requests.get(
        f"{CHATGPT_BASE}/api/auth/session",
        cookies=cookies,
        headers={"user-agent": USER_AGENT},
        impersonate=IMPERSONATE,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_session_from_access_token(access_token: str) -> dict[str, Any]:
    """
    如果已有 JWT access token，直接解码获取信息。

    :param access_token: ChatGPT session JWT
    :return: 包含 accessToken, accountId, email, userId, planType 的 dict
    """
    claims = decode_jwt_claims(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})

    return {
        "accessToken": access_token,
        "accountId": auth_info.get("chatgpt_account_id", ""),
        "userId": auth_info.get("chatgpt_user_id", ""),
        "email": profile.get("email", ""),
        "planType": auth_info.get("chatgpt_plan_type", "free"),
    }


def _agent_headers(access_token: str, env: Any | None = None) -> dict[str, str]:
    """构造 Agent API 请求头；有 BrowserSession 时使用该账号独立指纹画像。"""
    if env is not None and hasattr(env, "_get_common_headers"):
        headers = env._get_common_headers()
    else:
        headers = {"User-Agent": USER_AGENT}
    headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    })
    if env is not None:
        try:
            headers["oai-device-id"] = env.device_id
            headers["oai-language"] = env.navigator_language()
            if hasattr(env, "_attach_auth_rum_headers"):
                env._attach_auth_rum_headers(headers)
        except Exception:
            pass
    return headers


def _agent_post(url: str, *, access_token: str, payload: dict[str, Any], env: Any | None = None, timeout: int = 15):
    """使用独立 BrowserSession 或默认 curl_cffi 发起 Agent API POST。"""
    headers = _agent_headers(access_token, env=env)
    if env is not None and hasattr(env, "session"):
        return env.session.post(url, headers=headers, json=payload, timeout=timeout)
    return requests.post(url, headers=headers, json=payload, impersonate=IMPERSONATE, timeout=timeout)


# ============================================================
#  Agent 注册
# ============================================================

def register_agent(
    access_token: str,
    public_key_ssh: str,
    env: Any | None = None,
    timeout: int = 15,
    display_name: str | None = None,
) -> str:
    """
    在 auth.openai.com 注册 agent。

    :param access_token: ChatGPT session JWT
    :param public_key_ssh: SSH 格式的 Ed25519 公钥
    :return: agent_runtime_id
    """
    name = str(display_name or "").strip() or _random_agent_display_name()
    payload = {
        "abom": {
            "agent_version": AGENT_VERSION,
            "agent_harness_id": AGENT_HARNESS_ID,
            "running_location": RUNNING_LOCATION,
            # OpenAI Agent 场景也使用本轮随机名称，避免固定同一个展示名。
            "display_name": name,
            "agent_name": name,
        },
        "agent_public_key": public_key_ssh,
        "display_name": name,
        "agent_name": name,
        "name": name,
    }

    r = _agent_post(
        f"{AUTHAPI_BASE}/v1/agent/register",
        access_token=access_token,
        env=env,
        timeout=timeout,
        payload=payload,
    )

    # 兼容 OpenAI 接口严格校验未知字段的情况：如果命名字段不被接受，回退到原始协议。
    if r.status_code == 400 and any(x in (r.text or "").lower() for x in ("unknown", "unrecognized", "extra", "invalid")):
        _log("Step 3", f"OpenAI Agent 注册接口不接受名称字段，回退原始 payload: {r.text[:180]}", "WARN")
        r = _agent_post(
            f"{AUTHAPI_BASE}/v1/agent/register",
            access_token=access_token,
            env=env,
            timeout=timeout,
            payload={
                "abom": {
                    "agent_version": AGENT_VERSION,
                    "agent_harness_id": AGENT_HARNESS_ID,
                    "running_location": RUNNING_LOCATION,
                },
                "agent_public_key": public_key_ssh,
            },
        )

    if r.status_code != 200:
        raise RuntimeError(f"Agent registration failed: {r.status_code} {r.text}")

    data = r.json()
    agent_runtime_id = data.get("agent_runtime_id")
    if not agent_runtime_id:
        raise RuntimeError(f"No agent_runtime_id in response: {data}")

    return agent_runtime_id


# ============================================================
#  Task 注册（验证密钥对可用性）
# ============================================================

def register_task(
    access_token: str,
    agent_runtime_id: str,
    private_key_pkcs8_b64: str,
    env: Any | None = None,
    timeout: int = 15,
) -> str:
    """
    在 auth.openai.com 注册 task（验证密钥对可用性）。
    Codex CLI 启动时会自动执行此步骤。

    :param access_token: ChatGPT session JWT（仅用于验证，实际 Codex CLI 用密钥签名）
    :param agent_runtime_id: agent 运行时 ID
    :param private_key_pkcs8_b64: PKCS8 base64 私钥
    :return: encrypted_task_id
    """
    # 加载私钥
    pkcs8_der = base64.b64decode(private_key_pkcs8_b64)
    pem = b"-----BEGIN PRIVATE KEY-----\n" + base64.encodebytes(pkcs8_der) + b"-----END PRIVATE KEY-----\n"
    private_key = load_pem_private_key(pem, password=None)

    # 签名 payload: {agent_runtime_id}:{timestamp}
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = f"{agent_runtime_id}:{timestamp}"
    signature = private_key.sign(payload.encode())
    signature_b64 = base64.b64encode(signature).decode()

    r = _agent_post(
        f"{AUTHAPI_BASE}/v1/agent/{agent_runtime_id}/task/register",
        access_token=access_token,
        env=env,
        timeout=timeout,
        payload={
            "timestamp": timestamp,
            "signature": signature_b64,
        },
    )

    if r.status_code != 200:
        raise RuntimeError(f"Task registration failed: {r.status_code} {r.text}")

    data = r.json()
    return data.get("encrypted_task_id", "")


# ============================================================
#  auth.json 生成
# ============================================================

def _random_agent_display_name() -> str:
    """生成随机 Agent 名称，避免 sub2api 账号名直接暴露邮箱前缀。"""
    adjectives = [
        "Amber", "Blue", "Cedar", "Delta", "Echo", "Falcon", "Golden", "Harbor",
        "Ivory", "Jade", "Lunar", "Nova", "Orion", "Pine", "Quartz", "River",
        "Silver", "Summit", "Vertex", "Willow",
    ]
    nouns = [
        "Agent", "Bridge", "Comet", "Drift", "Field", "Garden", "Harbor", "Island",
        "Kernel", "Lantern", "Matrix", "Node", "Orbit", "Pilot", "Relay", "Signal",
        "Stone", "Tower", "Vector", "Worker",
    ]
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{random.choice(adjectives)} {random.choice(nouns)} {suffix}"


def generate_auth_json(
    agent_runtime_id: str,
    private_key_pkcs8_b64: str,
    account_id: str,
    chatgpt_user_id: str,
    email: str,
    plan_type: str = "free",
    chatgpt_account_is_fedramp: bool = False,
    display_name: str | None = None,
) -> dict[str, Any]:
    """
    生成 Codex CLI 的 auth.json。

    :return: auth.json dict
    """
    return {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": agent_runtime_id,
            "agent_private_key": private_key_pkcs8_b64,
            "account_id": account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "email": email,
            "name": email,
            "plan_type": plan_type,
            "chatgpt_account_is_fedramp": chatgpt_account_is_fedramp,
        },
    }


# ============================================================
#  sub2api 对接
# ============================================================

def build_sub2api_account_entry(
    auth_json: dict[str, Any],
    *,
    proxy_key: str | None = None,
) -> dict[str, Any]:
    """把 Codex Agent Identity auth.json 转成 sub2api accounts[] 条目。"""
    identity = auth_json.get("agent_identity") if isinstance(auth_json, dict) else None
    if not isinstance(identity, dict):
        raise ValueError("auth_json 缺少 agent_identity")

    agent_runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    agent_private_key = str(identity.get("agent_private_key") or "").strip()
    account_id = str(identity.get("account_id") or "").strip()
    chatgpt_user_id = str(identity.get("chatgpt_user_id") or "").strip()
    email = str(identity.get("email") or "").strip()
    display_name = email or str(identity.get("name") or identity.get("display_name") or "").strip() or f"agent-{agent_runtime_id[:8]}"
    plan_type = str(identity.get("plan_type") or "free").strip() or "free"
    if not agent_runtime_id or not agent_private_key:
        raise ValueError("agent_identity 缺少 agent_runtime_id/agent_private_key")

    entry = {
        "name": display_name,
        "platform": "openai",
        "type": "agent_identity",
        "credentials": {
            "agent_runtime_id": agent_runtime_id,
            "agent_private_key": agent_private_key,
            "account_id": account_id,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "email": email,
            "name": email,
            "plan_type": plan_type,
            "chatgpt_account_is_fedramp": bool(identity.get("chatgpt_account_is_fedramp", False)),
        },
        "extra": {
            "email": email,
            "account_id": account_id,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "agent_runtime_id": agent_runtime_id,
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "codex_agent",
        },
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }
    if proxy_key:
        entry["proxy_key"] = str(proxy_key)
    return entry


def _sub2api_dedupe_key(entry: dict[str, Any]) -> str:
    credentials = entry.get("credentials") if isinstance(entry.get("credentials"), dict) else {}
    extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
    agent_runtime_id = credentials.get("agent_runtime_id") or extra.get("agent_runtime_id")
    if agent_runtime_id:
        return f"agent:{agent_runtime_id}"
    user_id = credentials.get("chatgpt_user_id") or extra.get("chatgpt_user_id")
    account_id = credentials.get("chatgpt_account_id") or credentials.get("account_id") or extra.get("chatgpt_account_id") or extra.get("account_id")
    if user_id and account_id:
        return f"account-user:{user_id}|{account_id}"
    email = credentials.get("email") or extra.get("email")
    if email:
        return f"email:{email}"
    return ""


def upsert_sub2api_account(
    auth_json: dict[str, Any],
    output_path: str | os.PathLike[str],
    *,
    proxy_key: str | None = None,
) -> dict[str, Any]:
    """把 Agent Token 追加/更新到 sub2api.json。"""
    path = os.fspath(output_path)
    data: dict[str, Any]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("sub2api 配置文件根节点必须是对象")
        data = loaded
    else:
        data = {}

    accounts = data.setdefault("accounts", [])
    if not isinstance(accounts, list):
        raise ValueError("sub2api 配置中的 accounts 必须是数组")
    proxies = data.setdefault("proxies", [])
    if not isinstance(proxies, list):
        raise ValueError("sub2api 配置中的 proxies 必须是数组")
    if proxy_key and not proxies:
        data["proxies"] = [{"proxy_key": str(proxy_key)}]

    incoming = build_sub2api_account_entry(auth_json, proxy_key=proxy_key)
    key = _sub2api_dedupe_key(incoming)
    updated = False
    for idx, existing in enumerate(accounts):
        if isinstance(existing, dict) and key and _sub2api_dedupe_key(existing) == key:
            merged = dict(existing)
            merged.update(incoming)
            # 保留已人工调整的调度参数/代理键。
            for keep in ("concurrency", "priority", "rate_multiplier", "auto_pause_on_expired", "proxy_key"):
                if keep in existing and (keep != "proxy_key" or not proxy_key):
                    merged[keep] = existing[keep]
            accounts[idx] = merged
            updated = True
            break
    if not updated:
        accounts.append(incoming)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return {
        "ok": True,
        "updated": updated,
        "added": not updated,
        "path": os.path.abspath(path),
        "total": len(accounts),
        "email": incoming.get("extra", {}).get("email"),
        "dedupe_key": key,
    }


def upload_sub2api_account(
    auth_json: dict[str, Any],
    api_url: str,
    *,
    api_token: str | None = None,
    auth_header: str = "Authorization",
    auth_prefix: str = "Bearer",
    payload_mode: str = "accounts",
    proxy_key: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """通过 sub2api HTTP API 直接上传/导入 Agent Token。

    标准 Wei-Shaw/sub2api 使用：
      POST /api/v1/admin/accounts/import/codex-session
      {"contents":["<auth.json>"],"update_existing":true,...}

    payload_mode:
      - codex_session_import: sub2api 原生 Codex Session/Agent Identity 导入接口
      - account:  直接 POST 单个 account 对象
      - accounts: POST {"accounts": [account]}
      - config:   POST {"accounts": [account], "proxies": [...]}
    """
    url = str(api_url or "").strip()
    if not url:
        raise ValueError("SUB2API_API_BASE 为空，无法上传到 sub2api")

    mode = str(payload_mode or "accounts").strip().lower()
    incoming: dict[str, Any] | None = None
    if mode in {"codex_session_import", "codex-session-import", "import_codex_session"}:
        identity = auth_json.get("agent_identity") if isinstance(auth_json, dict) else {}
        payload = {
            "contents": [json.dumps(auth_json, ensure_ascii=False)],
            "name": str((identity or {}).get("email") or "").strip() or str((identity or {}).get("name") or "").strip() or _random_agent_display_name(),
            "update_existing": True,
            "concurrency": 3,
            "priority": 50,
            "confirm_mixed_channel_risk": True,
        }
    elif mode == "config":
        incoming = build_sub2api_account_entry(auth_json, proxy_key=proxy_key)
        payload = {"accounts": [incoming], "proxies": ([{"proxy_key": str(proxy_key)}] if proxy_key else [])}
    elif mode == "account":
        incoming = build_sub2api_account_entry(auth_json, proxy_key=proxy_key)
        payload = incoming
    else:
        incoming = build_sub2api_account_entry(auth_json, proxy_key=proxy_key)
        payload = {"accounts": [incoming]}

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "turb-gpt-free-register/sub2api",
    }
    token = str(api_token or "").strip()
    header_name = str(auth_header or "Authorization").strip() or "Authorization"
    prefix = str(auth_prefix or "").strip()
    if token:
        headers[header_name] = f"{prefix} {token}".strip() if prefix else token

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    status = int(getattr(resp, "status_code", 0) or 0)
    text = getattr(resp, "text", "") or ""
    try:
        body = resp.json()
    except Exception:
        body = {"text": text[:1000]}
    if status < 200 or status >= 300:
        raise RuntimeError(f"sub2api 上传失败 HTTP {status}: {text[:800]}")

    return {
        "ok": True,
        "uploaded": True,
        "url": url,
        "status_code": status,
        "payload_mode": mode,
        "email": (incoming or {}).get("extra", {}).get("email") or (
            (auth_json.get("agent_identity") or {}).get("email") if isinstance(auth_json, dict) else None
        ),
        "dedupe_key": _sub2api_dedupe_key(incoming) if incoming else (
            (auth_json.get("agent_identity") or {}).get("account_id") if isinstance(auth_json, dict) else None
        ),
        "response": body,
    }


# ============================================================
#  完整流程
# ============================================================

def create_codex_agent_identity(
    access_token: str,
    output_path: str | None = None,
    verify_task: bool = True,
    env: Any | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """
    完整流程：从 ChatGPT session JWT 创建 Codex Agent Identity auth.json。

    :param access_token: ChatGPT session JWT（从 /api/auth/session 获取的 accessToken）
    :param output_path: auth.json 输出路径，默认当前目录
    :param verify_task: 是否验证 task 注册（可选）
    :return: auth.json dict
    """
    _banner("Codex Agent Identity 注册开始")

    # Step 1: 解码 JWT 获取账号信息
    _log("Step 1", "解码 JWT 获取账号信息...")
    session = get_session_from_access_token(access_token)
    account_id = session["accountId"]
    chatgpt_user_id = session["userId"]
    email = session["email"]
    plan_type = session["planType"]

    if not account_id or not chatgpt_user_id:
        raise RuntimeError(f"JWT 缺少必要字段: account_id={account_id}, user_id={chatgpt_user_id}")

    _log("Step 1", f"account_id={account_id}", "OK")
    _log("Step 1", f"user_id={chatgpt_user_id}", "OK")
    _log("Step 1", f"email={email}", "OK")
    _log("Step 1", f"plan_type={plan_type}", "OK")

    # Step 2: 生成 Ed25519 密钥对
    _log("Step 2", "生成 Ed25519 密钥对...")
    private_key_b64, public_key_ssh = generate_ed25519_keypair()
    _log("Step 2", "Ed25519 私钥已生成（不输出私钥内容）", "OK")
    _log("Step 2", f"public_key_fingerprint={_fingerprint(public_key_ssh)}", "OK")

    # Step 3: 注册 agent
    agent_display_name = _random_agent_display_name()
    _log("Step 3", f"在 auth.openai.com 注册 agent，display_name={agent_display_name}...")
    agent_runtime_id = register_agent(access_token, public_key_ssh, env=env, timeout=timeout, display_name=agent_display_name)
    _log("Step 3", f"agent_runtime_id={agent_runtime_id}", "OK")

    # Step 4: 验证 task 注册（可选）
    if verify_task:
        _log("Step 4", "验证 task 注册...")
        try:
            task_id = register_task(access_token, agent_runtime_id, private_key_b64, env=env, timeout=timeout)
            _log("Step 4", f"task_id_fingerprint={_fingerprint(task_id)}", "OK")
        except Exception as e:
            _log("Step 4", f"验证失败（不影响 auth.json）: {e}", "WARN")

    # Step 5: 生成 auth.json
    _log("Step 5", "生成 auth.json...")
    auth_json = generate_auth_json(
        agent_runtime_id=agent_runtime_id,
        private_key_pkcs8_b64=private_key_b64,
        account_id=account_id,
        chatgpt_user_id=chatgpt_user_id,
        email=email,
        plan_type=plan_type,
        chatgpt_account_is_fedramp=False,
        display_name=email,
    )

    if output_path is None:
        output_path = os.path.join(os.getcwd(), "auth.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(auth_json, f, indent=2, ensure_ascii=False)

    _log("Step 5", f"已保存到 {output_path}", "OK")

    return auth_json


# ============================================================
#  入口
# ============================================================

def main() -> None:
    """
    使用方式：

    1. 直接传入 JWT：
       python codex_agent.py --token "eyJhbGci..."

    2. 传入 JSON 文件（包含 accessToken）：
       python codex_agent.py --file session.json

    3. 交互式输入：
       python codex_agent.py
    """

    import argparse

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Codex Agent Identity 自动注册")
    parser.add_argument("--token", type=str, help="ChatGPT session JWT (accessToken)")
    parser.add_argument("--file", type=str, help="包含 accessToken 的 JSON 文件路径")
    parser.add_argument("--output", "-o", type=str, default="auth.json", help="输出路径 (默认: auth.json)")
    parser.add_argument("--no-verify", action="store_true", help="跳过 task 注册验证")
    args = parser.parse_args()

    access_token = None

    if args.token:
        access_token = args.token
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
            access_token = data.get("accessToken") or data.get("access_token")
    else:
        # 交互式输入
        print("请输入 ChatGPT session JWT (accessToken)：")
        print("（从 chatgpt.com /api/auth/session 获取）")
        access_token = input("> ").strip()

    if not access_token:
        print("错误：未提供 access_token")
        sys.exit(1)

    create_codex_agent_identity(
        access_token=access_token,
        output_path=args.output,
        verify_task=not args.no_verify,
    )


if __name__ == "__main__":
    main()
