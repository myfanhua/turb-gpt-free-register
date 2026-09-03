"""Account-lifecycle network traffic measurement helpers.

Stages covered by the console registration pipeline:
  - browser: headed browser registration
  - totp: pure-protocol TOTP enrollment after success
  - trial: JP trial eligibility probe before/around persistence

Measurement is application-layer (request bodies + response bodies/Content-Length).
It is closer to full lifecycle usage than browser-only estimates, but still not a
proxy-billed exact figure (TLS/handshake overhead excluded).
"""
from __future__ import annotations

from typing import Any, Mapping


def empty_stage() -> dict[str, Any]:
    return {
        "request_count": 0,
        "response_count": 0,
        "failed_request_count": 0,
        "request_body_bytes": 0,
        "response_bytes": 0,
        "response_unknown_bytes_count": 0,
        "total_bytes": 0,
        "total_mib": 0.0,
        "total_mb": 0.0,
    }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_stage(raw: Mapping[str, Any] | None, *, name: str = "") -> dict[str, Any]:
    data = dict(raw or {})
    stage = empty_stage()
    for key in (
        "request_count",
        "response_count",
        "failed_request_count",
        "request_body_bytes",
        "response_bytes",
        "response_unknown_bytes_count",
    ):
        stage[key] = _as_int(data.get(key))
    total = stage["request_body_bytes"] + stage["response_bytes"]
    if _as_int(data.get("total_bytes")) > total:
        total = _as_int(data.get("total_bytes"))
    stage["total_bytes"] = total
    stage["total_mib"] = round(total / (1024 * 1024), 3)
    stage["total_mb"] = round(total / (1000 * 1000), 6)
    if name:
        stage["stage"] = name
    if data.get("measurement"):
        stage["measurement"] = str(data.get("measurement"))
    return stage


def merge_stages(stages: Mapping[str, Mapping[str, Any] | None] | None) -> dict[str, Any]:
    clean: dict[str, dict[str, Any]] = {}
    for name, raw in dict(stages or {}).items():
        key = str(name or "").strip() or "other"
        clean[key] = normalize_stage(raw, name=key)
    totals = empty_stage()
    for stage in clean.values():
        for key in (
            "request_count",
            "response_count",
            "failed_request_count",
            "request_body_bytes",
            "response_bytes",
            "response_unknown_bytes_count",
            "total_bytes",
        ):
            totals[key] += _as_int(stage.get(key))
    totals["total_mib"] = round(totals["total_bytes"] / (1024 * 1024), 3)
    totals["total_mb"] = round(totals["total_bytes"] / (1000 * 1000), 6)
    return {
        "stages": clean,
        "total_bytes": totals["total_bytes"],
        "total_mib": totals["total_mib"],
        "total_mb": totals["total_mb"],
        "request_count": totals["request_count"],
        "response_count": totals["response_count"],
        "failed_request_count": totals["failed_request_count"],
        "request_body_bytes": totals["request_body_bytes"],
        "response_bytes": totals["response_bytes"],
        "response_unknown_bytes_count": totals["response_unknown_bytes_count"],
        "measurement": "app_measured_body_plus_content_length",
        "unit_note": "total_mb uses decimal MB (1e6 bytes); total_mib uses MiB (2^20 bytes)",
    }


def attach_stage(
    traffic: Mapping[str, Any] | None,
    stage_name: str,
    stage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    current = dict(traffic or {})
    stages = dict(current.get("stages") or {})
    if stage:
        stages[stage_name] = normalize_stage(stage, name=stage_name)
    return merge_stages(stages)


class MeteredSession:
    """Wrap an HTTP session and accumulate request/response payload sizes."""

    def __init__(self, session: Any, *, stage: str = "protocol") -> None:
        self._session = session
        self.stage = stage
        self.stats = empty_stage()
        self.stats["stage"] = stage
        self.stats["measurement"] = "response_content_or_content_length"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def snapshot(self) -> dict[str, Any]:
        return normalize_stage(self.stats, name=self.stage)

    def _body_len(self, data: Any, json_body: Any) -> int:
        if data is None and json_body is None:
            return 0
        if isinstance(data, (bytes, bytearray)):
            return len(data)
        if data is not None:
            try:
                return len(str(data).encode("utf-8", errors="ignore"))
            except Exception:
                return 0
        if json_body is not None:
            try:
                import json

                return len(json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            except Exception:
                return 0
        return 0

    def _response_len(self, response: Any) -> tuple[int, bool]:
        # Prefer real body when available; fall back to Content-Length.
        try:
            content = getattr(response, "content", None)
            if content is not None:
                return max(0, len(content)), False
        except Exception:
            pass
        try:
            headers = getattr(response, "headers", {}) or {}
            if callable(headers):
                headers = headers()
            raw = headers.get("content-length") or headers.get("Content-Length")
            if raw is not None:
                return max(0, int(str(raw).strip())), False
        except Exception:
            pass
        return 0, True

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        req_len = self._body_len(kwargs.get("data"), kwargs.get("json"))
        self.stats["request_count"] += 1
        self.stats["request_body_bytes"] += req_len
        try:
            response = self._session.request(method, url, **kwargs)
        except Exception:
            self.stats["failed_request_count"] += 1
            self.stats["total_bytes"] = (
                self.stats["request_body_bytes"] + self.stats["response_bytes"]
            )
            raise
        resp_len, unknown = self._response_len(response)
        self.stats["response_count"] += 1
        if unknown:
            self.stats["response_unknown_bytes_count"] += 1
        else:
            self.stats["response_bytes"] += resp_len
        self.stats["total_bytes"] = (
            self.stats["request_body_bytes"] + self.stats["response_bytes"]
        )
        self.stats["total_mib"] = round(self.stats["total_bytes"] / (1024 * 1024), 3)
        self.stats["total_mb"] = round(self.stats["total_bytes"] / (1000 * 1000), 6)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)
