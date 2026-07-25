# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.chatgpt_conversation_protocol import (
    ChatGPTConversationProtocol, ConversationProtocolError, conversation_headers,
    parse_sse, require_auth, text_payload,
)


class Response:
    def __init__(self, data=None, status=200, chunks=()): self.data=data or {}; self.status_code=status; self._chunks=chunks; self.closed=False
    def json(self): return self.data
    def iter_content(self): return iter(self._chunks)
    def close(self): self.closed=True


class Session:
    sentinel_sid="device"; browser_profile={}
    def __init__(self, responses): self.responses=list(responses); self.calls=[]
    def get_chatgpt_headers(self, **_): return {"User-Agent":"fixture"}
    def post(self, url, **kwargs): self.calls.append((url,kwargs)); return self.responses.pop(0)


class Tests(unittest.TestCase):
    def test_payload_new_and_existing(self):
        self.assertNotIn("conversation_id", text_payload("x")); self.assertEqual(text_payload("x",conversation_id="c")["conversation_id"],"c")

    def test_chunked_sse_done_and_assistant_conversation_id(self):
        parsed=parse_sse([b'data: {"conversation_id":"c","message":{"author":{"role":"assistant"}}}\r\n\r\n',b'data: [DONE]\n\n'])
        self.assertTrue(parsed["done"]); self.assertTrue(parsed["assistant_completed"]); self.assertEqual(parsed["conversation_id"],"c")

    def test_headers_and_auth(self):
        h=conversation_headers({"User-Agent":"x"},{"token":"r","proof_token":"p"}); self.assertEqual(h["Accept"],"text/event-stream"); self.assertEqual(h["OpenAI-Sentinel-Proof-Token"],"p")
        with self.assertRaises(ConversationProtocolError): require_auth("",{})

    @patch("core.sentinel.generate_requirements_token", return_value="p")
    def test_real_request_shape_new_conversation_and_sse(self, _token):
        stream=Response(chunks=[b'data: {"conversation_id":"conv-1","message":{"author":{"role":"assistant"}}}\n\n',b'data: [DONE]\n\n'])
        session=Session([Response({"prepare_token":"pre","proofofwork":{"required":False}}), Response({"token":"req"}), stream])
        result=ChatGPTConversationProtocol(session,access_token="access",auth_artifacts={"cookies":{}},timeout=12).stream_message("hello")
        self.assertTrue(result.done); self.assertTrue(result.assistant_completed); self.assertEqual(result.conversation_id,"conv-1")
        self.assertTrue(session.calls[0][0].endswith("/sentinel/chat-requirements/prepare"))
        url, kwargs=session.calls[-1]; self.assertTrue(url.endswith("/backend-api/conversation")); self.assertTrue(kwargs["stream"]); self.assertEqual(kwargs["json"]["action"],"next")
        self.assertEqual(kwargs["headers"]["OpenAI-Sentinel-Chat-Requirements-Token"],"req")
        self.assertEqual(kwargs["headers"]["Authorization"],"Bearer access")

    @patch("core.sentinel.generate_requirements_token", return_value="p")
    def test_existing_conversation_and_missing_done_rejected(self, _token):
        session=Session([Response({"prepare_token":"pre"}),Response({"token":"req"}),Response(chunks=[b'data: {"conversation_id":"c"}\n\n'])])
        with self.assertRaisesRegex(ConversationProtocolError, r"\[DONE\]"):
            ChatGPTConversationProtocol(session,access_token="access",auth_artifacts={}).stream_message("hello",conversation_id="c")
        self.assertEqual(session.calls[-1][1]["json"]["conversation_id"],"c")

    @patch("core.sentinel.generate_requirements_token", return_value="p")
    def test_sse_error_timeout_and_http_are_explicit(self, _token):
        for final, expected in ((Response(chunks=[b'data: {"error":"blocked"}\n\n', b'data: [DONE]\n\n']), "SSE error"), (Response(status=401), "conversation HTTP 401")):
            session=Session([Response({"prepare_token":"pre"}),Response({"token":"req"}),final])
            with self.assertRaisesRegex(ConversationProtocolError, expected):
                ChatGPTConversationProtocol(session,access_token="access",auth_artifacts={}).stream_message("hello")


if __name__ == "__main__": unittest.main()
