# -*- coding: utf-8 -*-
"""默认关闭的注册后消息工作流；不包含任何猜测的 ChatGPT 生产接口。"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol
from config import post_register as cfg

CONTRACT_ERROR = "仅 ChatGPT 网页端会话：缺少已验证协议契约。请提供合法脱敏 HAR（网页端新会话、首/次条发送、正常/失败 SSE）；禁止以 Codex、OAuth、CPA 或开发者 API 替代。"

class WorkflowConfigError(ValueError): pass
class WorkflowTransportError(RuntimeError): pass

@dataclass
class WorkflowResult:
    status: str
    completed_count: int = 0
    conversation_id: str = ""
    errors: list[str] = field(default_factory=list)
    def summary(self): return {"status": self.status, "completed_count": self.completed_count, "conversation_id": self.conversation_id, "errors": self.errors[:3]}

class MessageTransport(Protocol):
    def send(self, *, message: str, conversation_id: str, timeout: int) -> Iterable[bytes | str]: ...

class DisabledTransport:
    def send(self, **_): raise WorkflowTransportError(CONTRACT_ERROR)

def parse_messages(message_list=None, message_count=None) -> list[str]:
    raw = cfg.MESSAGE_LIST if message_list is None else message_list
    count = cfg.MESSAGE_COUNT if message_count is None else message_count
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except json.JSONDecodeError as exc: raise WorkflowConfigError("MESSAGE_LIST 必须是 JSON 数组") from exc
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x.strip() for x in raw): raise WorkflowConfigError("MESSAGE_LIST 必须是非空字符串的 JSON 数组")
    if not isinstance(count, int) or count < 0: raise WorkflowConfigError("MESSAGE_COUNT 必须是非负整数")
    if count == 0: return []
    if count > len(raw): raise WorkflowConfigError("MESSAGE_COUNT 超过 MESSAGE_LIST 长度")
    return [x.strip() for x in raw[:count]]

def parse_sse(chunks: Iterable[bytes | str], *, deadline: float | None = None, clock=time.monotonic) -> tuple[bool, list[str]]:
    buffer = ""; errors=[]; done=False
    for chunk in chunks:
        if deadline is not None and clock() > deadline: raise TimeoutError("SSE 流超时")
        buffer += (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)).replace("\r\n", "\n")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            data = "\n".join(line[5:].strip() for line in event.splitlines() if line.startswith("data:"))
            if not data: continue
            if data == "[DONE]": done=True; continue
            try: payload=json.loads(data)
            except json.JSONDecodeError: continue
            if isinstance(payload, dict) and payload.get("error"): errors.append(str(payload["error"])[:180])
    if buffer.strip().startswith("data:"):
        data=buffer.strip()[5:].strip()
        if data == "[DONE]": done=True
    return done, errors

def run_workflow(*, transport: MessageTransport | None = None, enabled: bool | None = None, message_list=None, message_count=None, conversation_id: str | None = None, timeout: int | None = None) -> WorkflowResult:
    if not (cfg.POST_REGISTER_ENABLE if enabled is None else enabled): return WorkflowResult("skipped")
    messages=parse_messages(message_list, message_count); cid=conversation_id if conversation_id is not None else cfg.POST_REGISTER_CONVERSATION_ID
    transport=transport or DisabledTransport(); result=WorkflowResult("success", conversation_id=cid)
    for message in messages:
        try:
            seconds=max(1, int(timeout or cfg.POST_REGISTER_TIMEOUT)); deadline=time.monotonic()+seconds
            done, errors=parse_sse(transport.send(message=message, conversation_id=result.conversation_id, timeout=seconds), deadline=deadline)
            if errors or not done: raise WorkflowTransportError(errors[0] if errors else "SSE 流未收到 [DONE]")
            result.completed_count += 1
        except Exception as exc:
            result.errors.append(f"{type(exc).__name__}: {str(exc)[:180]}")
            result.status="partial" if result.completed_count else "failed"; return result
    return result
