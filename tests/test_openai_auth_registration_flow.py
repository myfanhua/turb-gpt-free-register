# -*- coding: utf-8 -*-
import json
import hashlib
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.openai_auth as openai_auth
from core.session import BrowserSession


class _Response:
    def __init__(self, *, status_code=200, url="", payload=None, text=""):
        self.status_code = status_code
        self.url = url
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ProtocolSession:
    def __init__(self):
        self.device_id = "device-id"
        self.sentinel_sid = "top-level-sid"
        self.sentinel_iframe_sid = "iframe-sid"
        self.document_navigation_id = "document-1"
        self.browser_profile = {"user_agent": "Test UA", "navigator_language": "ja-JP"}
        self.posts = []
        self.gets = []

    def get_auth_headers(self, referer=""):
        return {"referer": referer, "x-openai-document-navigation-id": self.document_navigation_id}

    def get_auth_navigate_headers(self, referer="", **_kwargs):
        return {"referer": referer}

    def get_sentinel_headers(self):
        return {"content-type": "text/plain;charset=UTF-8"}

    def get_sentinel_frame_headers(self, user_initiated=False):
        return {"sec-fetch-dest": "iframe", "user-initiated": user_initiated}

    def auth_cookie_header(self):
        return "oai-did=device-id"

    def rotate_document_navigation_id(self):
        old = self.document_navigation_id
        self.document_navigation_id = old + "-next"
        return self.document_navigation_id

    def post(self, url, *, headers, data):
        self.posts.append((url, headers, data))
        if url.endswith("/sentinel/req"):
            return _Response(payload={"token": "challenge", "persona": "p"})
        return _Response(payload={"continue_url": "/api/accounts/email-otp/send"})

    def get(self, url, *, headers, allow_redirects):
        self.gets.append((url, headers, allow_redirects))
        if "sentinel/frame.html" in url:
            return _Response(url=url)
        return _Response(url="https://auth.openai.com/email-verification")


class OpenAIRegistrationFlowTests(unittest.TestCase):
    def setUp(self):
        self.sdk = patch.object(openai_auth, 'resolve_sentinel_sdk',
                                return_value=Path(__file__).resolve().parents[1] / 'sentinel' / 'sdk.js')
        self.sdk.start()
        self.addCleanup(self.sdk.stop)

    def test_network_preflight_uses_address_bar_navigation_shape(self):
        class Session:
            def __init__(self):
                self.header_args = []
                self.requests = []

            def get_chatgpt_navigate_headers(self, referer="", **kwargs):
                self.header_args.append((referer, kwargs))
                return {"sec-fetch-site": "none"}

            def get(self, url, *, headers, allow_redirects, timeout):
                self.requests.append((url, headers, allow_redirects, timeout))
                return _Response(url=url)

        session = Session()
        openai_auth.network_preflight(session)

        self.assertEqual(session.header_args, [("", {})])
        self.assertEqual(session.requests[0][0], "https://chatgpt.com/auth/login")
        self.assertEqual(session.requests[0][1]["sec-fetch-site"], "none")
        self.assertEqual(session.requests[0][3], 12.0)

    def test_network_preflight_retries_status_403_on_same_session(self):
        class Session:
            def __init__(self):
                self.calls = 0
                self.reset_count = 0

            def get_chatgpt_navigate_headers(self, referer=""):
                return {"referer": referer}

            def get(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _Response(status_code=403, text="challenge")
                return _Response()

            def reset_circuit_breaker(self):
                self.reset_count += 1

        session = Session()
        with patch.object(openai_auth, "_interruptible_sleep") as sleep:
            openai_auth.network_preflight(session)

        self.assertEqual(session.calls, 2)
        self.assertEqual(session.reset_count, 1)
        sleep.assert_called_once_with(1.0)

    def test_sentinel_iframe_retries_socks5_proxy_failure(self):
        class ProxyError(RuntimeError):
            pass

        class Session(_ProtocolSession):
            def __init__(self):
                super().__init__()
                self.frame_attempts = 0
                self.reset_count = 0

            def get(self, url, *, headers, allow_redirects):
                if "sentinel/frame.html" in url:
                    self.frame_attempts += 1
                    if self.frame_attempts == 1:
                        raise ProxyError(
                            "curl: (97) cannot complete SOCKS5 connection to sentinel.openai.com"
                        )
                return super().get(url, headers=headers, allow_redirects=allow_redirects)

            def reset_circuit_breaker(self):
                self.reset_count += 1

        session = Session()
        with patch.object(openai_auth, "_interruptible_sleep") as sleep:
            result = openai_auth.request_sentinel_token(session, "username_password_create")

        self.assertEqual(result["token"], "challenge")
        self.assertEqual(session.frame_attempts, 2)
        self.assertEqual(session.reset_count, 1)
        sleep.assert_called_once_with(1.0)

    def test_bundled_sentinel_sdk_matches_captured_20260810913b_source(self):
        sdk_path = Path(__file__).resolve().parents[1] / "sentinel" / "sdk.js"
        self.assertEqual(
            hashlib.sha256(sdk_path.read_bytes()).hexdigest(),
            "49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd",
        )

    def test_register_user_matches_password_registration_request(self):
        session = _ProtocolSession()

        result = openai_auth.register_user(
            session, "user@example.com", "Password!123", "sentinel", "so-token"
        )

        url, headers, body = session.posts[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/user/register")
        self.assertEqual(headers["referer"], "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["openai-sentinel-token"], "sentinel")
        self.assertEqual(headers["openai-sentinel-so-token"], "so-token")
        self.assertEqual(json.loads(body), {
            "username": "user@example.com",
            "password": "Password!123",
        })
        self.assertEqual(result["continue_url"], "/api/accounts/email-otp/send")

    def test_navigate_create_account_password_switches_from_passwordless_page(self):
        class Session(_ProtocolSession):
            def get(self, url, *, headers, allow_redirects):
                self.gets.append((url, headers, allow_redirects))
                return _Response(url="https://auth.openai.com/create-account/password")

        session = Session()
        final_url = openai_auth.navigate_create_account_password(
            session, "https://auth.openai.com/email-verification"
        )

        url, headers, redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["referer"], "https://auth.openai.com/email-verification")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertTrue(redirects)
        self.assertEqual(final_url, "https://auth.openai.com/create-account/password")
        self.assertEqual(session.document_navigation_id, "document-1-next")

    def test_email_otp_send_accepts_relative_url_and_rotates_document_id(self):
        session = _ProtocolSession()

        final_url = openai_auth.navigate_email_otp_send(
            session, "/api/accounts/email-otp/send"
        )

        url, headers, redirects = session.gets[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/send")
        self.assertEqual(headers["referer"], "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        self.assertTrue(redirects)
        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertEqual(session.document_navigation_id, "document-1-next")

    def test_validate_email_otp_forwards_both_sentinel_headers(self):
        session = _ProtocolSession()

        openai_auth.validate_email_otp(session, "123456", "sentinel", "so-token")

        url, headers, body = session.posts[-1]
        self.assertEqual(url, "https://auth.openai.com/api/accounts/email-otp/validate")
        self.assertEqual(headers["openai-sentinel-token"], "sentinel")
        self.assertEqual(headers["openai-sentinel-so-token"], "so-token")
        self.assertEqual(json.loads(body), {"code": "123456"})

    def test_sentinel_flow_selects_matching_sdk_context_and_sid(self):
        session = _ProtocolSession()
        captured = []

        def fake_requirements(**kwargs):
            captured.append((kwargs['sentinel_sid'], kwargs['browser_profile']))
            return "p-value"

        with patch.object(openai_auth, "generate_sentinel_requirements", side_effect=fake_requirements):
            openai_auth.request_sentinel_token(session, "username_password_create")
            openai_auth.request_sentinel_token(session, "email_otp_validate")
            openai_auth.request_sentinel_token(session, "oauth_create_account")

        # 每个业务 flow 重新执行 SDK requirements，不跨 flow 复用初始 p。
        self.assertEqual([item[0] for item in captured], [
            "iframe-sid", "top-level-sid", "top-level-sid",
        ])
        self.assertEqual(
            captured[0][1]["script_src_samples"],
            ["https://sentinel.openai.com/sentinel/20260810913b/sdk.js"],
        )
        self.assertIn("/sentinel/", captured[1][1]["script_src_samples"][0])
        self.assertTrue(captured[1][1]["script_src_samples"][0].endswith("/sdk.js"))
        self.assertIsNone(captured[0][1]["build_id"])
        self.assertIsNone(captured[1][1]["build_id"])
        flows = [json.loads(call[2])["flow"] for call in session.posts]
        self.assertEqual(flows, [
            "username_password_create", "email_otp_validate", "oauth_create_account",
        ])
        frame_gets = [call for call in session.gets if "sentinel/frame.html" in call[0]]
        self.assertEqual(len(frame_gets), 2)
        self.assertTrue(frame_gets[0][1]["user-initiated"])
        self.assertFalse(frame_gets[1][1]["user-initiated"])

    def test_password_page_sentinel_bundle_reuses_same_iframe_p(self):
        session = _ProtocolSession()
        with patch.object(openai_auth, "generate_sentinel_requirements", return_value="shared-p") as generate:
            result = openai_auth.request_password_sentinel_bundle(session)

        self.assertEqual(result["token"], "challenge")
        generate.assert_called_once()
        flows = []
        values = []
        for url, _headers, body in session.posts:
            if url.endswith("/sentinel/req"):
                payload = json.loads(body)
                flows.append(payload["flow"])
                values.append(payload["p"])
        self.assertEqual(flows, [
            "email_otp_validate",
            "username_password_create",
            "authorize_continue",
        ])
        self.assertEqual(values, ["shared-p", "shared-p", "shared-p"])
        frame_gets = [call for call in session.gets if "sentinel/frame.html" in call[0]]
        self.assertEqual(len(frame_gets), 1)
        self.assertTrue(frame_gets[0][1]["user-initiated"])

    def test_header_runner_uses_the_same_sid_as_challenge_request(self):
        session = _ProtocolSession()
        output = json.dumps({"c": "challenge", "so": "session-observer"})

        with patch.object(openai_auth, "generate_sentinel_token", return_value=output) as generate:
            header, so_header = openai_auth.build_sentinel_header(
                session, {"token": "challenge"}, "username_password_create"
            )
            self.assertEqual(generate.call_args.kwargs["sentinel_sid"], "iframe-sid")
            self.assertEqual(json.loads(header), {"c": "challenge"})
            self.assertEqual(json.loads(so_header)["flow"], "username_password_create")

            openai_auth.build_sentinel_header(
                session, {"token": "challenge"}, "email_otp_validate"
            )
            self.assertEqual(generate.call_args.kwargs["sentinel_sid"], "top-level-sid")

    def test_browser_session_keeps_document_id_until_explicit_navigation(self):
        session = object.__new__(BrowserSession)
        session.document_navigation_id = "document-a"
        session.datadog_trace_id = "1"
        session.datadog_parent_id = "2"
        session.datadog_origin = "rum"

        first = session._attach_auth_rum_headers({})
        second = session._attach_auth_rum_headers({})
        self.assertEqual(first["x-openai-document-navigation-id"], "document-a")
        self.assertEqual(second["x-openai-document-navigation-id"], "document-a")
        self.assertNotEqual(first["x-access-flow-invocation-id"], second["x-access-flow-invocation-id"])
        self.assertNotEqual(first["x-datadog-trace-id"], second["x-datadog-trace-id"])
        self.assertNotEqual(first["x-datadog-parent-id"], second["x-datadog-parent-id"])

        rotated = session.rotate_document_navigation_id()
        third = session._attach_auth_rum_headers({})
        self.assertNotEqual(rotated, "document-a")
        self.assertEqual(third["x-openai-document-navigation-id"], rotated)


if __name__ == "__main__":
    unittest.main()
