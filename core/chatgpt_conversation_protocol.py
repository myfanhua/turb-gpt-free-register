"""普通 ChatGPT 网页 conversation 的窄协议 adapter。

协议结构参考 chatgpt2api tracked ``services/openai_backend_api.py``（仅
conversation / chat-requirements / SSE 部分）；本模块没有运行时外部依赖，且
复用本项目 ``BrowserSession``、Sentinel 指纹和已保存的 ChatGPT 登录态。
不包含图片、Codex、Developer API、账号池或 Web 服务。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

CONVERSATION_PATH = "/backend-api/conversation"
_BASE = "https://chatgpt.com"


class ConversationProtocolError(RuntimeError):
    """可展示的协议失败；不得携带 token/cookie/完整响应正文。"""
    def __init__(self, reason: str, *, stage: str = "unknown", http_status: int | None = None):
        super().__init__(reason); self.stage = stage; self.http_status = http_status; self.reason = reason


@dataclass(frozen=True)
class ConversationCompletion:
    conversation_id: str
    done: bool
    assistant_completed: bool
    error: str = ""


def text_payload(message: str, *, model: str = "auto", conversation_id: str = "", parent_message_id: str = "") -> dict:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message 必须是非空文本")
    payload = {
        "action": "next",
        "messages": [{"id": str(uuid.uuid4()), "author": {"role": "user"}, "content": {"content_type": "text", "parts": [message]}}],
        "model": model,
        "parent_message_id": parent_message_id or str(uuid.uuid4()),
        "conversation_mode": {"kind": "primary_assistant"},
        "conversation_origin": None,
        "force_use_sse": True,
        "history_and_training_disabled": True,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return payload


def parse_sse(chunks: Iterable[bytes | str]) -> dict:
    """增量消费 SSE，只有 ``[DONE]`` 才视为完成。"""
    buffer = ""
    conversation_id = ""
    done = False
    assistant_completed = False
    errors: list[str] = []
    for raw in chunks:
        buffer += (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).replace("\r\n", "\n")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
            if data == "[DONE]":
                done = True
                continue
            try:
                obj = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            conversation_id = str(obj.get("conversation_id") or conversation_id)
            if obj.get("error"):
                errors.append(str(obj["error"])[:180])
            message = obj.get("message") or {}
            if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
                assistant_completed = True
    return {"done": done, "conversation_id": conversation_id, "assistant_completed": assistant_completed, "errors": errors}


def disabled_reason() -> str:
    return "PROTOCOL_CONVERSATION_ENABLE=False；默认不会发起 conversation 网络请求"


def conversation_headers(base_headers: dict, requirements: dict) -> dict:
    out = dict(base_headers or {})
    out.update({"Accept": "text/event-stream", "Content-Type": "application/json"})
    token = str(requirements.get("token") or "")
    if not token:
        raise ConversationProtocolError("缺少 Sentinel requirements token")
    out["OpenAI-Sentinel-Chat-Requirements-Token"] = token
    for source, target in (("proof_token", "OpenAI-Sentinel-Proof-Token"), ("turnstile_token", "OpenAI-Sentinel-Turnstile-Token"), ("so_token", "OpenAI-Sentinel-SO-Token")):
        if requirements.get(source):
            out[target] = str(requirements[source])
    return out


def require_auth(access_token: str, auth_artifacts: dict | None):
    if not str(access_token or "").strip():
        raise ConversationProtocolError("缺少已保存的 ChatGPT access_token")
    if not isinstance(auth_artifacts, dict):
        raise ConversationProtocolError("缺少已保存的 ChatGPT auth_artifacts/session cookies")


class ChatGPTConversationProtocol:
    """最小的已登录网页端协议实现；session 可注入以便完整离线测试。"""

    def __init__(self, session, *, access_token: str, auth_artifacts: dict, timeout: int = 30):
        require_auth(access_token, auth_artifacts)
        self.session, self.access_token, self.auth_artifacts = session, access_token.strip(), auth_artifacts
        self.timeout = int(timeout)

    def _headers(self, *, sse: bool = False) -> dict:
        getter = getattr(self.session, "get_chatgpt_headers", None)
        headers = dict(getter(referer="https://chatgpt.com/") if getter else {})
        headers["Authorization"] = self.access_token if self.access_token.lower().startswith("bearer ") else f"Bearer {self.access_token}"
        if sse:
            headers["Accept"] = "text/event-stream"
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _json(response, stage: str) -> dict:
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise ConversationProtocolError(f"{stage} HTTP {status}", stage=stage, http_status=status)
        try:
            value = response.json()
        except Exception as exc:
            raise ConversationProtocolError(f"{stage} 返回非 JSON", stage=stage) from exc
        if not isinstance(value, dict):
            raise ConversationProtocolError(f"{stage} 返回格式无效", stage=stage)
        return value

    def chat_requirements(self) -> dict:
        """执行 prepare/finalize；PoW 优先复用当前 ``core.sentinel``。"""
        from core.sentinel import generate_requirements_token, get_enforcement_token
        sid = getattr(self.session, "sentinel_sid", getattr(self.session, "device_id", ""))
        profile = getattr(self.session, "browser_profile", None)
        p_token = generate_requirements_token(sid, profile=profile)
        prefix = _BASE + "/backend-api/sentinel/chat-requirements"
        prepare = self._json(self.session.post(prefix + "/prepare", headers=self._headers(sse=False), json={"p": p_token}, timeout=self.timeout), "chat requirements prepare")
        turnstile = prepare.get("turnstile") or {}
        if turnstile.get("required"):
            raise ConversationProtocolError("chat requirements 需要 Turnstile；当前登录态缺少已验证 token", stage="chat_requirements", http_status=None)
        proof = ""
        pow_data = prepare.get("proofofwork") or {}
        if pow_data.get("required"):
            proof = get_enforcement_token(prepare, "", "", sid, profile=profile)
        finalized = self._json(self.session.post(prefix + "/finalize", headers=self._headers(sse=False), json={"prepare_token": prepare.get("prepare_token", ""), "proof_token": proof, "turnstile_token": ""}, timeout=self.timeout), "chat requirements finalize")
        token = str(finalized.get("token") or "")
        if not token:
            raise ConversationProtocolError("chat requirements finalize 缺少 token", stage="chat_requirements_finalize")
        return {"token": token, "proof_token": proof, "turnstile_token": "", "so_token": finalized.get("so_token", "")}

    def stream_message(self, message: str, *, conversation_id: str = "", model: str = "auto") -> ConversationCompletion:
        requirements = self.chat_requirements()
        headers = conversation_headers(self._headers(sse=True), requirements)
        response = self.session.post(_BASE + CONVERSATION_PATH, headers=headers, json=text_payload(message, model=model, conversation_id=conversation_id), timeout=self.timeout, stream=True)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise ConversationProtocolError(f"conversation HTTP {status}", stage="conversation", http_status=status)
        try:
            chunks = response.iter_content() if hasattr(response, "iter_content") else getattr(response, "iter_lines")()
            parsed = parse_sse(chunks)
        except TimeoutError as exc:
            raise ConversationProtocolError("conversation SSE 超时", stage="conversation_sse") from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close): close()
        if parsed["errors"]:
            raise ConversationProtocolError("conversation SSE error", stage="conversation_sse")
        if not parsed["done"]:
            raise ConversationProtocolError("conversation SSE 未收到 [DONE]", stage="conversation_sse")
        return ConversationCompletion(parsed["conversation_id"] or conversation_id, True, bool(parsed["assistant_completed"]))
