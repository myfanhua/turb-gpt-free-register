# -*- coding: utf-8 -*-
"""Local HTTP proxy bridge: Roxy -> local bridge -> Clash -> upstream proxy."""
from __future__ import annotations

import base64
import select
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


class ProxyBridgeError(RuntimeError):
    pass


def parse_proxy_url(raw: str, *, sid: str = "") -> ProxyEndpoint:
    value = str(raw or "").strip().replace("{sid}", str(sid or ""))
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if not parsed.hostname or not parsed.port:
        raise ValueError("invalid proxy URL")
    return ProxyEndpoint(
        scheme=(parsed.scheme or "http").lower(),
        host=parsed.hostname,
        port=int(parsed.port),
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def build_proxy_authorization(username: str, password: str) -> bytes:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n".encode("ascii")


def build_connect_request(host: str, port: int, username: str = "", password: str = "") -> bytes:
    target = f"{host}:{int(port)}"
    auth = build_proxy_authorization(username, password) if username else b""
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode("ascii") + auth + b"\r\n"


def _basic_credentials(request: bytes) -> tuple[str, str]:
    for line in request.split(b"\r\n"):
        if not line.lower().startswith(b"proxy-authorization:"):
            continue
        value = line.split(b":", 1)[1].strip()
        if not value.lower().startswith(b"basic "):
            return "", ""
        try:
            decoded = base64.b64decode(value.split(None, 1)[1], validate=True).decode("utf-8")
        except Exception:
            return "", ""
        username, sep, password = decoded.partition(":")
        return (username, password) if sep else (decoded, "")
    return "", ""


def extract_local_sid(request: bytes) -> str:
    username, _ = _basic_credentials(request)
    for prefix in ("sid-", "sid_"):
        if username.startswith(prefix):
            return username[len(prefix) :].strip()
    return ""


def rewrite_proxy_authorization(request: bytes, upstream_url: str, *, sid: str) -> bytes:
    endpoint = parse_proxy_url(upstream_url, sid=sid)
    lines = request.split(b"\r\n")
    out: list[bytes] = []
    inserted = False
    for line in lines:
        if line.lower().startswith(b"proxy-authorization:"):
            if endpoint.username and not inserted:
                out.append(build_proxy_authorization(endpoint.username, endpoint.password).rstrip(b"\r\n"))
                inserted = True
            continue
        if line == b"" and endpoint.username and not inserted:
            out.append(build_proxy_authorization(endpoint.username, endpoint.password).rstrip(b"\r\n"))
            inserted = True
        out.append(line)
    return b"\r\n".join(out)


def local_proxy_url(host: str, port: int, sid: str) -> str:
    return f"http://{quote('sid-' + sid, safe='')}:{quote('bridge', safe='')}@{host}:{int(port)}"


def _recv_until(sock: socket.socket, marker: bytes = b"\r\n\r\n", max_bytes: int = 131072) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ProxyBridgeError("HEADER_TOO_LARGE")
    return bytes(data)


def _status_code(header: bytes) -> int:
    try:
        return int(header.splitlines()[0].split()[1])
    except Exception:
        return 0


def _connect_via_chain(upstream: ProxyEndpoint, chain: ProxyEndpoint, timeout: float) -> socket.socket:
    try:
        sock = socket.create_connection((chain.host, chain.port), timeout=timeout)
        sock.settimeout(timeout)
    except OSError as exc:
        raise ProxyBridgeError(f"CLASH_PREPROXY_UNAVAILABLE: {type(exc).__name__}") from exc
    try:
        sock.sendall(build_connect_request(upstream.host, upstream.port, chain.username, chain.password))
        response = _recv_until(sock)
        if _status_code(response) != 200:
            raise ProxyBridgeError(f"CLASH_CONNECT_FAILED: HTTP {_status_code(response) or '-'}")
        return sock
    except Exception:
        sock.close()
        raise


def _relay(left: socket.socket, right: socket.socket, idle_timeout: float) -> None:
    left.settimeout(None)
    right.settimeout(None)
    sockets = [left, right]
    last_activity = time.monotonic()
    while True:
        readable, _, errored = select.select(sockets, [], sockets, 1.0)
        if errored:
            return
        if not readable:
            if idle_timeout > 0 and time.monotonic() - last_activity > idle_timeout:
                return
            continue
        for current in readable:
            other = right if current is left else left
            try:
                chunk = current.recv(65536)
                if not chunk:
                    return
                other.sendall(chunk)
                last_activity = time.monotonic()
            except OSError:
                return


def _send_502(client: socket.socket, marker: str) -> None:
    safe = str(marker or "PROXY_CHAIN_ERROR").replace("\r", " ").replace("\n", " ")[:240]
    body = safe.encode("utf-8", errors="replace")
    try:
        client.sendall(
            b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
    except OSError:
        pass


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        upstream_sock: socket.socket | None = None
        try:
            client.settimeout(self.server.connect_timeout)  # type: ignore[attr-defined]
            first_request = _recv_until(client)
            if not first_request:
                return
            sid = extract_local_sid(first_request)
            if not sid:
                raise ProxyBridgeError("SID_MISSING")
            upstream = parse_proxy_url(self.server.upstream_template, sid=sid)  # type: ignore[attr-defined]
            chain = parse_proxy_url(self.server.preproxy)  # type: ignore[attr-defined]
            upstream_sock = _connect_via_chain(upstream, chain, self.server.connect_timeout)  # type: ignore[attr-defined]
            upstream_sock.sendall(rewrite_proxy_authorization(first_request, self.server.upstream_template, sid=sid))  # type: ignore[attr-defined]
            response_header = _recv_until(upstream_sock)
            status = _status_code(response_header)
            if status == 407:
                raise ProxyBridgeError("UPSTREAM_PROXY_AUTH_FAILED")
            if not response_header:
                raise ProxyBridgeError("UPSTREAM_PROXY_CLOSED")
            client.sendall(response_header)
            if status >= 400:
                return
            _relay(client, upstream_sock, self.server.idle_timeout)  # type: ignore[attr-defined]
        except ProxyBridgeError as exc:
            _send_502(client, str(exc))
        except Exception as exc:
            _send_502(client, f"PROXY_CHAIN_ERROR: {type(exc).__name__}")
        finally:
            try:
                client.close()
            except OSError:
                pass
            if upstream_sock is not None:
                try:
                    upstream_sock.close()
                except OSError:
                    pass


class BridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        preproxy: str,
        upstream_template: str,
        connect_timeout: float = 20.0,
        idle_timeout: float = 180.0,
    ):
        self.preproxy = preproxy
        self.upstream_template = upstream_template
        self.connect_timeout = float(connect_timeout)
        self.idle_timeout = float(idle_timeout)
        super().__init__(address, _BridgeHandler)


def start_bridge(
    host: str,
    port: int,
    *,
    preproxy: str,
    upstream_template: str,
    connect_timeout: float = 20.0,
    idle_timeout: float = 180.0,
) -> BridgeServer:
    server = BridgeServer(
        (host, int(port)),
        preproxy=preproxy,
        upstream_template=upstream_template,
        connect_timeout=connect_timeout,
        idle_timeout=idle_timeout,
    )
    threading.Thread(target=server.serve_forever, name="proxy-chain-bridge", daemon=True).start()
    return server
