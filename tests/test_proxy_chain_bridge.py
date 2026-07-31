# -*- coding: utf-8 -*-
import base64
import socket
import threading
import unittest

from tools.proxy_chain_bridge import (
    build_connect_request,
    extract_local_sid,
    parse_proxy_url,
    rewrite_proxy_authorization,
    BridgeServer,
)


class ProxyChainBridgeTests(unittest.TestCase):
    def test_parse_proxy_url_and_render_sid(self):
        endpoint = parse_proxy_url("http://user-region-KR-sid-{sid}-t-3:pass@proxy.example:3000", sid="abc123")
        self.assertEqual(endpoint.host, "proxy.example")
        self.assertEqual(endpoint.port, 3000)
        self.assertEqual(endpoint.username, "user-region-KR-sid-abc123-t-3")
        self.assertEqual(endpoint.password, "pass")

    def test_extract_local_sid_from_proxy_authorization(self):
        token = base64.b64encode(b"sid-abc123:bridge").decode("ascii")
        request = f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
        self.assertEqual(extract_local_sid(request), "abc123")

    def test_rewrite_proxy_authorization_uses_upstream_credentials(self):
        local = base64.b64encode(b"sid-abc123:bridge").decode("ascii")
        request = f"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: Basic {local}\r\n\r\n".encode()
        rewritten = rewrite_proxy_authorization(request, "http://user:pass@proxy.example:3000", sid="abc123")
        upstream = base64.b64encode(b"user:pass").decode("ascii")
        self.assertIn(f"Proxy-Authorization: Basic {upstream}".encode(), rewritten)
        self.assertNotIn(b"sid-abc123", rewritten)

    def test_build_connect_request_contains_target(self):
        request = build_connect_request("proxy.example", 3000)
        self.assertTrue(request.startswith(b"CONNECT proxy.example:3000 HTTP/1.1"))
        self.assertTrue(request.endswith(b"\r\n\r\n"))

    def test_connect_is_chained_and_upstream_auth_is_rewritten(self):
        ready = threading.Event()
        captured = {}

        def fake_clash():
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            captured["address"] = server.getsockname()
            ready.set()
            conn, _ = server.accept()
            with conn:
                first = conn.recv(4096)
                captured["connect"] = first
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                second = conn.recv(4096)
                captured["request"] = second
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\nhello")
            server.close()

        thread = threading.Thread(target=fake_clash, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        chain_host, chain_port = captured["address"]
        bridge = BridgeServer(
            ("127.0.0.1", 0),
            preproxy=f"http://{chain_host}:{chain_port}",
            upstream_template="http://up-user:up-pass@upstream.example:3000",
            connect_timeout=2,
            idle_timeout=2,
        )
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        try:
            with socket.create_connection(bridge.server_address, timeout=3) as client:
                local = base64.b64encode(b"sid-abc123:bridge").decode("ascii")
                client.sendall(
                    f"CONNECT target.example:443 HTTP/1.1\r\nHost: target.example:443\r\nProxy-Authorization: Basic {local}\r\n\r\n".encode()
                )
                response = client.recv(4096)
            self.assertIn(b"HTTP/1.1 200 Connection Established", response)
            self.assertIn(b"CONNECT upstream.example:3000", captured["connect"])
            upstream_auth = base64.b64encode(b"up-user:up-pass").decode("ascii").encode()
            self.assertIn(upstream_auth, captured["request"])
            self.assertNotIn(b"sid-abc123", captured["request"])
        finally:
            bridge.shutdown()
            bridge.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
