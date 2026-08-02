import unittest
from types import SimpleNamespace

from core import roxy_codex_oauth


class _Response:
    status_code = 302


class _Session:
    def __init__(self, status_code=302):
        self.calls = []
        self.status_code = status_code

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = _Response()
        response.status_code = self.status_code
        return response


class RoxyCodexTokenProxyTests(unittest.TestCase):
    def test_roxy_registration_proxy_takes_priority_for_token_exchange(self):
        opened = SimpleNamespace(
            registration_proxy="http://sid-browser:bridge@127.0.0.1:25001"
        )

        result = roxy_codex_oauth._resolve_token_exchange_proxy(
            opened,
            "http://different-upstream.example:3000",
        )

        self.assertEqual(
            result,
            "http://sid-browser:bridge@127.0.0.1:25001",
        )

    def test_explicit_proxy_is_fallback_when_profile_has_no_recorded_proxy(self):
        opened = SimpleNamespace(registration_proxy=None)

        result = roxy_codex_oauth._resolve_token_exchange_proxy(
            opened,
            "socks5h://explicit.example:3000",
        )

        self.assertEqual(result, "socks5h://explicit.example:3000")

    def test_token_transport_preflight_uses_token_endpoint_without_redirects(self):
        session = _Session()

        status = roxy_codex_oauth._preflight_token_exchange_transport(
            session,
            "https://auth.openai.com/oauth/token",
        )

        self.assertEqual(status, 302)
        self.assertEqual(
            session.calls,
            [
                (
                    "https://auth.openai.com/oauth/token",
                    {"allow_redirects": False},
                )
            ],
        )

    def test_token_transport_preflight_rejects_proxy_gateway_status(self):
        session = _Session(status_code=407)

        with self.assertRaisesRegex(RuntimeError, "status=407"):
            roxy_codex_oauth._preflight_token_exchange_transport(
                session,
                "https://auth.openai.com/oauth/token",
            )


if __name__ == "__main__":
    unittest.main()
