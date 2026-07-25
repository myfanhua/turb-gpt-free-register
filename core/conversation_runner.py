"""Conversation Pool runner：对既有 protocol 栈的薄 adapter。

未来实现必须复用 ``core.session.BrowserSession``（curl_cffi/TLS/cookie jar/headers）、
``config.browser``/``config.openai_protocol``、``account_export.fetch_session``、
``chatgpt_bootstrap`` 与已保存的 auth_artifacts；HAR 只补 conversation endpoint/payload/SSE。
不得重建认证、TLS、cookie 或 header 体系。
"""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass
from typing import Protocol
from core import conversation_manager as manager

HAR_REQUIRED = ("url", "method", "headers", "new_conversation_payload", "existing_conversation_payload", "sse_done", "sse_error_timeout")
DISABLED = "PROTOCOL_CONVERSATION_ENABLE=False；默认禁止 ChatGPT conversation 网络请求"


def _safe_reason(value: object, limit: int = 160) -> str:
    """生成可写入 binding 的脱敏诊断，绝不持久化认证材料或响应正文。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(
        r"(?i)\b(access[_-]?token|authorization|cookie|set-cookie|bearer|password|otp|email|query_url)\b\s*[:=]\s*[^\s;,]+",
        r"\1=[redacted]",
        text,
    )
    return text[:limit] or "未知错误"

def validate_capture_contract(value: dict) -> list[str]:
    return [key for key in HAR_REQUIRED if not value.get(key)] if isinstance(value, dict) else list(HAR_REQUIRED)

class Transport(Protocol):
    def create_conversation(self, *, account_id:int) -> str: ...
    def send_message(self, *, account_id:int, conversation_id:str, message:str) -> object: ...
    def await_completion(self, event:object, *, timeout:int) -> bool: ...

class ProtocolChatGPTWebTransport:
    """首选薄 adapter：注入既有 BrowserSession/auth artifacts/bootstrap 上下文，不创建 requests client。

    默认关闭。仅在进程环境显式开启 ``PROTOCOL_CONVERSATION_ENABLE=true`` 后才会
    发送；底层协议实现经过 fixture 覆盖，不依赖 HAR 或外部项目运行时。
    """
    def __init__(self, contract=None, *, access_token="", auth_artifacts=None, adapter=None, session=None, session_factory=None):
        self.contract=contract or {}; self.access_token=access_token; self.auth_artifacts=auth_artifacts
        self.adapter=adapter; self.session=session; self.session_factory=session_factory
    def _disabled(self): raise RuntimeError(DISABLED)
    def _ready(self):
        from config import conversation_pool as cfg
        if not cfg.PROTOCOL_CONVERSATION_ENABLE: self._disabled()
        from core.chatgpt_conversation_protocol import require_auth
        require_auth(self.access_token,self.auth_artifacts)
        if self.adapter is None:
            from core.chatgpt_conversation_protocol import ChatGPTConversationProtocol
            session = self.session or (self.session_factory() if self.session_factory else self._session_from_artifacts())
            self.adapter = ChatGPTConversationProtocol(session, access_token=self.access_token, auth_artifacts=self.auth_artifacts)

    def _load_saved_auth(self, account_id: int) -> None:
        """Runner 的唯一账号读取点：只取本项目已保存的网页登录态。"""
        if self.access_token and isinstance(self.auth_artifacts, dict):
            return
        from core import db
        row = db.get_account(account_id)
        if not row:
            raise RuntimeError("Conversation Pool 账号不存在")
        raw = row.get("extra_json") or "{}"
        try:
            extra = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            extra = {}
        artifacts = extra.get("auth_artifacts") if isinstance(extra, dict) else {}
        self.access_token = str(row.get("access_token") or (artifacts or {}).get("access_token") or "")
        self.auth_artifacts = artifacts if isinstance(artifacts, dict) else {}
        self._proxy = row.get("proxy_used") or ""
        self._browser_profile = extra.get("browser_profile") if isinstance(extra, dict) else None
        self._sentinel_sid = str((extra or {}).get("sentinel_sid") or "")

    def _session_from_artifacts(self):
        """只复用项目 BrowserSession；不新建 requests/TLS 栈。"""
        from core.session import BrowserSession
        # 恢复注册时已保存的出口和画像，避免用新随机画像/代理破坏网页登录态。
        session = BrowserSession(proxy=getattr(self, "_proxy", ""))
        if isinstance(getattr(self, "_browser_profile", None), dict):
            session.browser_profile = dict(self._browser_profile)
        if getattr(self, "_sentinel_sid", ""):
            session.sentinel_sid = self._sentinel_sid
        cookies = self.auth_artifacts.get("cookies") or {}
        if isinstance(cookies, dict):
            for name, value in cookies.items(): session.session.cookies.set(name, str(value), domain="chatgpt.com", path="/")
        elif isinstance(cookies, list):
            for item in cookies:
                if isinstance(item, dict) and item.get("name") and item.get("value"):
                    session.session.cookies.set(item["name"], str(item["value"]), domain=item.get("domain") or "chatgpt.com", path=item.get("path") or "/")
        return session

    def create_conversation(self, **kw):
        self._load_saved_auth(int(kw["account_id"]))
        self._ready()
        # ChatGPT 在第一条 conversation 请求中创建会话；不要猜测另一个创建 endpoint。
        return ""

    def send_message(self, *, conversation_id="", message, **kw):
        self._load_saved_auth(int(kw["account_id"]))
        self._ready()
        return self.adapter.stream_message(message, conversation_id=conversation_id)

    def await_completion(self, event, *, timeout=30):
        from core.chatgpt_conversation_protocol import ConversationCompletion
        if not isinstance(event, ConversationCompletion): return False
        return event.done and event.assistant_completed

class BrowserAssistTransport(ProtocolChatGPTWebTransport):
    """仅可选辅助：已有浏览器驱动产生的上下文可用于恢复；不是必要 fallback。"""

def run_binding(account_id:int, template_id:str, transport:Transport|None=None, timeout=30, retry=False, message_delay=None):
    row=manager.claim(account_id, template_id, retry=retry)
    if not row: return None
    data=manager._load(); template=data["templates"][template_id]; transport=transport or ProtocolChatGPTWebTransport()
    try:
        cid=row.get("conversation_id") or transport.create_conversation(account_id=account_id)
        manager.checkpoint(account_id,template_id,conversation_id=cid)
        first_send = True
        for idx in range(int(row.get("current_index",0)), len(template["messages"])):
            # 风控抑制：两条消息之间按调用方给定的随机间隔等待，模拟真人读完再发问。
            if not first_send and message_delay:
                try:
                    wait = float(message_delay() or 0)
                except Exception:
                    wait = 0
                if wait > 0: time.sleep(wait)
            first_send = False
            event=transport.send_message(account_id=account_id, conversation_id=cid, message=template["messages"][idx])
            if transport.await_completion(event, timeout=timeout) is not True:
                raise RuntimeError("transport 未确认完成")
            event_conversation_id = getattr(event, "conversation_id", "")
            if event_conversation_id:
                cid = event_conversation_id
            manager.checkpoint(account_id,template_id,current_index=idx+1,conversation_id=cid,status="running")
        return manager.checkpoint(
            account_id,
            template_id,
            status="completed",
            conversation_id=cid,
            last_error="",
            stage="completed",
            http_status=None,
            reason="",
        )
    except Exception as exc:
        current=manager.get_binding(account_id, template_id) or row
        from core.chatgpt_conversation_protocol import ConversationProtocolError
        if isinstance(exc, ConversationProtocolError):
            stage = _safe_reason(exc.stage, 64)
            http_status = exc.http_status if isinstance(exc.http_status, int) else None
            reason = _safe_reason(exc.reason)
            requires_browser_verification = stage == "chat_requirements" and "turnstile" in reason.lower()
        else:
            stage = "transport"
            http_status = None
            reason = _safe_reason(type(exc).__name__)
            requires_browser_verification = False
        status = (
            "needs_browser_verification"
            if requires_browser_verification
            else ("partial" if int(current.get("current_index",0)) else "failed")
        )
        error = f"stage={stage}; http={http_status if http_status is not None else 'none'}; reason={reason}"
        return manager.checkpoint(
            account_id,
            template_id,
            status=status,
            last_error=error,
            stage=stage,
            http_status=http_status,
            reason=reason,
        )
