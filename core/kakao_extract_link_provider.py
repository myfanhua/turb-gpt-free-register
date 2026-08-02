# -*- coding: utf-8 -*-
"""Kakao Pay 异步批量提链 API 客户端。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - 仅在精简运行环境触发
    curl_requests = None


ERROR_MESSAGES = {
    "CDK_INVALID": "CDK 无效、已停用或已过期",
    "CDK_QUOTA_EXHAUSTED": "CDK 次数已用完",
    "CDK_QUOTA_INSUFFICIENT": "CDK 剩余次数不足以覆盖本批账号",
}


class KakaoExtractLinkError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        http_status: int | None = None,
        transient: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.transient = transient


@dataclass(frozen=True)
class KakaoAcceptedBatch:
    batch_id: str
    request_id: str = ""
    status: str = "queued"


@dataclass(frozen=True)
class KakaoBatchResult:
    batch_id: str
    status: str
    done: bool
    results: list[dict]
    success_count: int = 0
    failure_count: int = 0
    charged_count: int = 0
    remaining_count: int | None = None


Transport = Callable[[str, str, dict | None, float], tuple[int, dict]]


class KakaoExtractLinkClient:
    def __init__(
        self,
        *,
        api_base: str,
        cdk: str,
        timeout_seconds: int = 930,
        poll_interval: float = 4.0,
        request_timeout: float = 30.0,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_poll_failures: int = 3,
    ):
        self.api_base = str(api_base or "").strip().rstrip("/")
        self.cdk = str(cdk or "").strip()
        self.timeout_seconds = max(30, min(1200, int(timeout_seconds or 930)))
        self.poll_interval = max(0.0, float(poll_interval or 0.0))
        self.request_timeout = max(1.0, float(request_timeout or 30.0))
        self.transport = transport or self._default_transport
        self.sleep = sleep
        self.monotonic = monotonic
        self.max_poll_failures = max(0, int(max_poll_failures or 0))
        self._secrets = {value for value in (self.cdk,) if value}

        if not self.api_base:
            raise ValueError("KAKAO_EXTRACT_API_BASE 为空")
        if not self.cdk:
            raise ValueError("KAKAO_EXTRACT_CDK/CDK 为空")

    def _sanitize(self, value) -> str:
        message = str(value or "").strip()
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                message = message.replace(secret, "***")
        message = re.sub(
            r"(?P<scheme>https?://)[^\s/@:]+:[^\s/@]+@",
            r"\g<scheme>***:***@",
            message,
            flags=re.IGNORECASE,
        )
        return message[:500]

    @staticmethod
    def _error_parts(payload: dict) -> tuple[str, str]:
        raw_error = payload.get("error")
        code = ""
        detail = ""
        if isinstance(raw_error, dict):
            code = str(raw_error.get("code") or raw_error.get("error") or "").strip()
            detail = str(
                raw_error.get("message")
                or raw_error.get("detail")
                or raw_error.get("reason")
                or ""
            ).strip()
        elif raw_error:
            code = str(raw_error).strip()
        detail = str(
            detail
            or payload.get("detail")
            or payload.get("message")
            or payload.get("reason")
            or ""
        ).strip()
        return code, detail

    def _http_error(self, status: int, payload: dict) -> KakaoExtractLinkError:
        code, detail = self._error_parts(payload)
        if code in ERROR_MESSAGES:
            message = ERROR_MESSAGES[code]
        elif status == 404 and "batch not found" in detail.lower():
            message = "批次不存在或已被服务端清理"
        elif detail:
            message = detail
        elif code:
            message = code
        else:
            message = f"Kakao 提链接口 HTTP {status}"
        return KakaoExtractLinkError(
            self._sanitize(message),
            code=code,
            http_status=status,
            transient=status == 429 or status >= 500,
        )

    def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.api_base}{path}"
        try:
            status, data = self.transport(method, url, payload, self.request_timeout)
        except KakaoExtractLinkError:
            raise
        except Exception as exc:
            raise KakaoExtractLinkError(
                self._sanitize(exc),
                transient=isinstance(exc, (TimeoutError, OSError)),
            ) from exc
        if not isinstance(data, dict):
            data = {"detail": str(data or "")}
        if int(status) < 200 or int(status) >= 300:
            raise self._http_error(int(status), data)
        return data

    @staticmethod
    def _default_transport(
        method: str,
        url: str,
        payload: dict | None,
        timeout: float,
    ) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if curl_requests is not None:
            response = curl_requests.request(
                method,
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            try:
                data = response.json()
            except Exception:
                data = {"detail": (response.text or "")[:500]}
            return int(response.status_code), data if isinstance(data, dict) else {"detail": str(data)}

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=headers, method=method)
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            data = json.loads(raw or "{}")
            return int(response.status), data if isinstance(data, dict) else {"detail": str(data)}

    def submit(self, access_tokens: list[str]) -> KakaoAcceptedBatch:
        tokens = [str(token or "").strip() for token in access_tokens]
        if not 1 <= len(tokens) <= 5 or any(not token for token in tokens):
            raise ValueError("Kakao 每批 accessTokens 数量必须为 1-5")
        self._secrets.update(tokens)
        data = self._request_json(
            "POST",
            "/api/v1/extractions/async",
            {
                "accessTokens": tokens,
                "cdk": self.cdk,
                "timeoutSeconds": self.timeout_seconds,
            },
        )
        batch_id = str(data.get("batchId") or "").strip()
        if not batch_id:
            raise KakaoExtractLinkError("Kakao 提链服务响应缺少 batchId")
        return KakaoAcceptedBatch(
            batch_id=batch_id,
            request_id=str(data.get("requestId") or "").strip(),
            status=str(data.get("status") or "queued").strip() or "queued",
        )

    def get_batch(self, batch_id: str) -> KakaoBatchResult:
        clean_batch_id = str(batch_id or "").strip()
        if not clean_batch_id:
            raise ValueError("batchId 为空")
        data = self._request_json(
            "GET",
            f"/api/v1/extractions/{quote(clean_batch_id, safe='')}",
        )
        status = str(data.get("status") or "").strip().lower()
        done = bool(data.get("done")) or status in {"completed", "error"}
        results = data.get("results") if isinstance(data.get("results"), list) else []
        remaining = data.get("remainingCount")
        try:
            remaining_count = int(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            remaining_count = None
        return KakaoBatchResult(
            batch_id=str(data.get("batchId") or clean_batch_id),
            status=status or ("completed" if done else "running"),
            done=done,
            results=[item if isinstance(item, dict) else {"success": False, "error": str(item)} for item in results],
            success_count=int(data.get("successCount") or 0),
            failure_count=int(data.get("failureCount") or 0),
            charged_count=int(data.get("chargedCount") or 0),
            remaining_count=remaining_count,
        )

    def poll(
        self,
        batch_id: str,
        *,
        on_update: Callable[[KakaoBatchResult], None] | None = None,
    ) -> KakaoBatchResult:
        deadline = self.monotonic() + self.timeout_seconds + 60
        failures = 0
        while True:
            try:
                result = self.get_batch(batch_id)
                failures = 0
            except KakaoExtractLinkError as exc:
                if not exc.transient or failures >= self.max_poll_failures:
                    raise
                failures += 1
                if self.poll_interval:
                    self.sleep(self.poll_interval * failures)
                continue
            if on_update is not None:
                on_update(result)
            if result.done:
                return result
            if self.monotonic() >= deadline:
                raise KakaoExtractLinkError("Kakao 提链批次轮询超时")
            if self.poll_interval:
                self.sleep(self.poll_interval)
