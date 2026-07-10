# -*- coding: utf-8 -*-
"""
注册成功后的"自动触发 flow"模块。

主流程在 main.py / registration_service.py 拿到 access_token 后调用：
    trigger_flow(access_token)
不论触发成功失败，注册主流程都视为成功。
"""
import json
import logging

# 这是个内部 HTTP 接口（156.225.31.95），无 Cloudflare 拦截，
# 不需要 curl_cffi 的 TLS 指纹模拟，直接用标准 requests 库。
import requests

# 用模块属性方式读 config，支持 WebUI 热加载（config.reload_all()）。
from config import flow_trigger as _cfg
from config.browser import USER_AGENT, ACCEPT_LANGUAGE  # 浏览器指纹固定，不需要热加载

logger = logging.getLogger(__name__)


def _flow_result(
    *,
    status: str,
    ok: bool = False,
    http_status: int | None = None,
    flow_id: str | None = None,
    message: str = "",
) -> dict:
    return {
        "status": status,
        "ok": ok,
        "http_status": http_status,
        "flow_id": flow_id,
        "message": message,
    }


def _build_headers() -> dict:
    return {
        "Accept": "*/*",
        "Accept-Language": ACCEPT_LANGUAGE,
        "Authorization": f"Bearer {_cfg.FLOW_TRIGGER_BEARER}",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Origin": "http://162.211.183.196:8888",
        "Pragma": "no-cache",
        "Referer": "http://162.211.183.196:8888/",
        "User-Agent": USER_AGENT,
        "Cookie": _cfg.FLOW_TRIGGER_COOKIE,
    }


def _send_sync(access_token: str) -> dict:
    """实际同步执行 HTTP POST，并返回可统计的结果。"""
    if not access_token:
        return _flow_result(status="skipped", message="access_token 为空")

    body = dict(_cfg.FLOW_TRIGGER_PAYLOAD)
    body["access_token"] = access_token

    try:
        resp = requests.post(
            _cfg.FLOW_TRIGGER_URL,
            headers=_build_headers(),
            json=body,
            timeout=_cfg.FLOW_TRIGGER_TIMEOUT,
            verify=False,
        )
    except Exception as exc:
        return _flow_result(status="failed", message=f"{type(exc).__name__}: {exc}")

    # 简单解析 flow_id 打个日志，触发结果不影响主流程
    flow_id = ""
    response_preview = (resp.text or "")[:200]
    try:
        data = resp.json()
        flow_id = (data.get("flow") or {}).get("flow_id", "")
    except Exception:
        pass

    if resp.status_code == 200:
        return _flow_result(
            status="success",
            ok=True,
            http_status=resp.status_code,
            flow_id=flow_id or None,
            message=response_preview,
        )

    return _flow_result(
        status="failed",
        http_status=resp.status_code,
        flow_id=flow_id or None,
        message=response_preview,
    )


def trigger_flow(access_token: str) -> dict:
    """
    触发 156.225 flow 接口并返回结果。

    Args:
        access_token: 本次注册拿到的 ChatGPT access_token
    """
    if not _cfg.ENABLE_FLOW_TRIGGER:
        return _flow_result(status="skipped", message="ENABLE_FLOW_TRIGGER=False")

    if not access_token:
        return _flow_result(status="skipped", message="access_token 为空")

    return _send_sync(access_token)
