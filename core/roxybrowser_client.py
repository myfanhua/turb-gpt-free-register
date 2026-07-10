# -*- coding: utf-8 -*-
"""RoxyBrowser 本地 API 客户端。"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import requests

from config import roxybrowser as _cfg

logger = logging.getLogger(__name__)


@dataclass
class RoxyOpenResult:
    profile_id: str
    raw: dict
    debugger_address: str | None = None
    webdriver_url: str | None = None
    ws_endpoint: str | None = None
    created_by_run: bool = False


def _strip_slashes(value: str) -> str:
    return str(value or "").strip().strip("/")


def _join_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or "").strip())
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{host}{port}"
    return str(proxy_url or "").strip()


def _proxy_url_to_roxy_info(proxy_url: str) -> dict:
    """
    将 config/proxy.py 里的代理 URL 转成 Roxy /browser/create 的 proxyInfo。

    支持：
      http://user:pass@host:port
      https://user:pass@host:port
      socks5://user:pass@host:port
      socks5h://user:pass@host:port  -> Roxy 侧按 SOCKS5 处理
    """
    text = str(proxy_url or "").strip()
    if not text:
        raise ValueError("代理为空")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"Roxy 暂不支持该代理协议: {scheme or '-'}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"代理格式缺少 host/port: {_mask_proxy(text)}")

    protocol = {
        "http": "HTTP",
        "https": "HTTPS",
        "socks5": "SOCKS5",
        "socks5h": "SOCKS5",
    }[scheme]
    # Roxy /browser/create 官方字段是：
    # proxyMethod / proxyCategory / ipType / protocol / host / port / proxyUserName / proxyPassword / checkChannel
    # 之前误用了 proxyType/proxyHost/proxyPort/proxyAccount，Roxy 会忽略，导致创建窗口实际未设置代理。
    info = {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": protocol,
        "ipType": "IPV4",
        "protocol": protocol,
        "host": parsed.hostname,
        "port": str(parsed.port),
    }
    if parsed.username:
        info["proxyUserName"] = unquote(parsed.username)
    if parsed.password:
        info["proxyPassword"] = unquote(parsed.password)
    check_channel = str(getattr(_cfg, "ROXY_PROXY_CHECK_CHANNEL", "") or "").strip()
    if check_channel:
        info["checkChannel"] = check_channel
    return info


def _dig(payload: dict, *keys: str):
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first(payload: dict, paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        value = _dig(payload, *path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _workspace_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_WORKSPACE_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _project_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_PROJECT_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


class RoxyBrowserClient:
    def __init__(self, api_base: str | None = None, token: str | None = None):
        self.api_base = (api_base or _cfg.ROXY_API_BASE).strip()
        self.token = (token if token is not None else _cfg.ROXY_API_TOKEN).strip()
        self.http = requests.Session()
        if self.token:
            # 官方文档要求所有接口请求头必须加 token。这里同时兼容 token / Authorization。
            self.http.headers.update({
                "token": self.token,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "timeout" in text
            or "timed out" in text
            or "connection" in text
            or "temporarily" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "http 429" in text
        )

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = _join_url(self.api_base, path)
        method_u = method.upper()
        # create 超时后服务端可能已创建环境，直接重试可能产生孤儿环境；默认不重试 create。
        is_create = str(path or "").rstrip("/").endswith("/create") or "browser/create" in str(path or "")
        max_attempts = 1 if is_create else max(1, int(getattr(_cfg, "ROXY_API_RETRIES", 3) or 3))
        base_delay = max(0.5, float(getattr(_cfg, "ROXY_API_RETRY_DELAY", 2) or 2))
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "[Roxy] %s %s params=%s body=%s attempt=%s/%s",
                    method, url, params, json_body, attempt, max_attempts,
                )
                resp = self.http.request(
                    method_u,
                    url,
                    params=params or None,
                    json=json_body if json_body is not None else None,
                    timeout=max(5, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                )
                text = resp.text or ""
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": text}
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(f"Roxy API 请求失败 {method_u} {path} HTTP {resp.status_code}: {text[:500]}")
                if isinstance(payload, dict):
                    code = payload.get("code")
                    ok = payload.get("ok")
                    success = payload.get("success")
                    if code not in (None, 0, 200, "0", "200") and ok is not True and success is not True:
                        msg = payload.get("msg") or payload.get("message") or payload.get("error") or json.dumps(payload, ensure_ascii=False)[:500]
                        raise RuntimeError(f"Roxy API 返回失败 {method_u} {path}: {msg}")
                if attempt > 1:
                    logger.info("[Roxy] API 重试成功：%s %s attempt=%s/%s", method_u, path, attempt, max_attempts)
                return payload if isinstance(payload, dict) else {"data": payload}
            except Exception as exc:
                last_exc = exc
                retryable = self._is_retryable_error(exc)
                if attempt >= max_attempts or not retryable:
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Roxy] API 请求失败，将在 %.1fs 后重试：%s %s attempt=%s/%s error=%s",
                    delay, method_u, path, attempt, max_attempts, exc,
                )
                time.sleep(delay)
        raise last_exc or RuntimeError(f"Roxy API 请求失败 {method_u} {path}")

    def try_request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> tuple[bool, dict | str]:
        """宽松请求：用于探测不同 Roxy 版本接口，失败不抛出。"""
        try:
            return True, self.request(method, path, params=params, json_body=json_body)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _extract_workspace_items(payload: dict) -> list[dict]:
        """解析 /browser/workspace：团队 rows + project_details 项目列表；兼容递归兜底。"""
        out = []

        # 官方结构：data.rows[].id/workspaceName/project_details[].projectId/projectName
        rows = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("list") or data.get("records")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                wid = row.get("id") or row.get("workspaceId") or row.get("workspace_id")
                wname = row.get("workspaceName") or row.get("workspace_name") or row.get("name") or str(wid or "")
                projects = row.get("project_details") or row.get("projectDetails") or row.get("projects") or []
                if isinstance(projects, list) and projects:
                    for proj in projects:
                        if not isinstance(proj, dict):
                            continue
                        pid = proj.get("projectId") or proj.get("project_id") or proj.get("id")
                        pname = proj.get("projectName") or proj.get("project_name") or proj.get("name") or str(pid or "")
                        if wid:
                            out.append({
                                "id": str(wid),
                                "name": str(wname),
                                "projectId": str(pid or ""),
                                "projectName": str(pname or ""),
                                "label": f"{wname} / {pname} ({wid}/{pid})" if pid else f"{wname} ({wid})",
                                "raw": {"workspace": row, "project": proj},
                            })
                elif wid:
                    out.append({
                        "id": str(wid),
                        "name": str(wname),
                        "projectId": "",
                        "projectName": "",
                        "label": f"{wname} ({wid})",
                        "raw": row,
                    })

        if out:
            return out

        # 兜底：递归抽 workspace/team/company 结构。
        def pick_id_name(item: dict) -> tuple[str, str]:
            wid = _first(item, [
                ("workspaceId",), ("workspace_id",), ("workspaceID",),
                ("teamId",), ("team_id",), ("teamID",),
                ("companyId",), ("company_id",), ("orgId",), ("org_id",),
                ("id",), ("value",), ("key",),
            ])
            name = _first(item, [
                ("workspaceName",), ("workspace_name",),
                ("teamName",), ("team_name",),
                ("companyName",), ("company_name",),
                ("orgName",), ("org_name",),
                ("name",), ("label",), ("title",), ("remark",),
            ])
            return wid, name

        def looks_like_workspace(item: dict) -> bool:
            keys = {str(k).lower() for k in item.keys()}
            joined = " ".join(keys)
            return any(x in joined for x in ("workspace", "team", "company", "org")) or ("id" in keys and "name" in keys)

        def walk(node):
            if isinstance(node, dict):
                wid, name = pick_id_name(node)
                if wid and looks_like_workspace(node):
                    out.append({"id": wid, "name": name or wid, "projectId": "", "projectName": "", "label": f"{name or wid} ({wid})", "raw": node})
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        dedup = {}
        for item in out:
            raw_keys = {str(k).lower() for k in (item.get("raw") or {}).keys()}
            if "dirid" in raw_keys and not any(k in raw_keys for k in ("workspaceid", "teamid", "companyid")):
                continue
            key = f"{item.get('id')}::{item.get('projectId','')}"
            dedup[key] = item
        return list(dedup.values())

    def list_workspaces(self) -> dict:
        """
        获取 Roxy 团队/工作区列表。
        Roxy 不同版本路径可能有差异，因此先试配置路径，再试常见路径。
        """
        configured = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_PATH", "") or "").strip()
        method = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_METHOD", "GET") or "GET").upper()
        candidates = []
        if configured:
            candidates.append((method, configured))
        candidates.extend([
            ("GET", "/browser/workspace"),
            ("POST", "/browser/workspace"),
            ("GET", "/workspace/list"),
            ("POST", "/workspace/list"),
            ("GET", "/workspace"),
            ("POST", "/workspace"),
            ("GET", "/team/list"),
            ("POST", "/team/list"),
            ("GET", "/team"),
            ("POST", "/team"),
            ("GET", "/workspaces"),
            ("GET", "/teams"),
            ("GET", "/user/workspace/list"),
            ("POST", "/user/workspace/list"),
            ("GET", "/user/team/list"),
            ("POST", "/user/team/list"),
            ("GET", "/api/workspace/list"),
            ("POST", "/api/workspace/list"),
            ("GET", "/api/team/list"),
            ("POST", "/api/team/list"),
            ("GET", "/browser/workspace/list"),
            ("POST", "/browser/workspace/list"),
            ("GET", "/browser/team/list"),
            ("POST", "/browser/team/list"),
        ])

        errors = []
        seen = set()
        for m, path in candidates:
            key = (m, path)
            if key in seen:
                continue
            seen.add(key)
            ok, payload = self.try_request(m, path)
            if not ok:
                errors.append({"method": m, "path": path, "error": payload})
                continue
            items = self._extract_workspace_items(payload if isinstance(payload, dict) else {})
            if items:
                return {"ok": True, "path": path, "method": m, "items": items, "raw": payload}
            errors.append({"method": m, "path": path, "error": "响应中未解析到团队/工作区列表", "payload": payload})

        return {"ok": False, "items": [], "errors": errors}

    def create_profile(self, payload: dict | None = None) -> str:
        body = dict(getattr(_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}) or {})
        default_os = str(getattr(_cfg, "ROXY_DEFAULT_OS", "macOS") or "macOS").strip()
        if default_os:
            # Roxy 官方枚举为 macOS（大小写敏感），默认创建 macOS 指纹环境。
            body.setdefault("os", default_os)
        default_os_version = str(getattr(_cfg, "ROXY_DEFAULT_OS_VERSION", "") or "").strip()
        if default_os_version:
            body.setdefault("osVersion", default_os_version)
        workspace_id = _workspace_id_value()
        if workspace_id:
            # Roxy 官方 /browser/create 要求 workspaceId。
            body.setdefault("workspaceId", workspace_id)
        project_id = _project_id_value()
        if project_id:
            body.setdefault("projectId", project_id)
        if bool(getattr(_cfg, "ROXY_CREATE_USE_PROXY_POOL", False)) and not body.get("proxyInfo"):
            from config import proxy as _proxy_cfg

            proxy_url = _proxy_cfg.pick_proxy()
            if proxy_url:
                proxy_info = _proxy_url_to_roxy_info(proxy_url)
                body["proxyInfo"] = proxy_info
                logger.info(
                    "[Roxy] 创建环境启用代理池：proxy=%s type=%s host=%s port=%s",
                    _mask_proxy(proxy_url),
                    proxy_info.get("protocol") or proxy_info.get("proxyCategory"),
                    proxy_info.get("host"),
                    proxy_info.get("port"),
                )
            else:
                logger.warning("[Roxy] 已启用 ROXY_CREATE_USE_PROXY_POOL，但 PROXY_POOL 为空，本次创建环境不设置代理")
        if payload:
            body.update(payload)
        if not body.get("workspaceId"):
            raise RuntimeError(
                "Roxy 创建环境需要 workspaceId。请在 config/roxybrowser.py 或 WebUI 的 RoxyBrowser 配置中填写 ROXY_WORKSPACE_ID，"
                "或直接在 ROXY_PROFILE_CREATE_PAYLOAD 里加入 {'workspaceId': '你的工作区ID'}。"
            )
        logger.info("[Roxy] 创建环境参数：workspaceId=%s projectId=%s os=%s osVersion=%s", body.get("workspaceId"), body.get("projectId") or "-", body.get("os") or "-", body.get("osVersion") or "-")
        result = self.request(_cfg.ROXY_CREATE_METHOD, _cfg.ROXY_CREATE_PATH, json_body=body)
        profile_id = _first(result, [
            ("id",), ("dirId",), ("dir_id",), ("profile_id",), ("profileId",), ("browser_id",),
            ("data", "id"), ("data", "dirId"), ("data", "dir_id"),
            ("data", "profile_id"), ("data", "profileId"), ("data", "browser_id"),
        ])
        if not profile_id:
            raise RuntimeError(f"Roxy 创建环境成功但未返回 dirId/profile_id: {result}")
        return profile_id

    @staticmethod
    def _normalize_profile_id(value: str | None) -> str:
        text = str(value or "").strip()
        # WebUI/人工配置里常用 - 表示“未配置”，这里统一按空处理。
        if text in ("-", "—", "无", "空", "none", "None", "null", "NULL"):
            return ""
        return text

    def open_profile(self, profile_id: str | None = None) -> RoxyOpenResult:
        one_profile = bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
        configured_pid = self._normalize_profile_id(profile_id if profile_id is not None else getattr(_cfg, "ROXY_PROFILE_ID", ""))
        if one_profile and configured_pid:
            raise RuntimeError(
                "已启用 ROXY_ONE_PROFILE_PER_ACCOUNT=True（一号一环境），"
                "不能配置/传入固定 ROXY_PROFILE_ID；请留空以便每个账号创建新环境。"
            )

        pid = configured_pid
        created_by_run = False
        if not pid:
            pid = self.create_profile()
            created_by_run = True
            logger.info("[Roxy] 已创建临时环境：%s", pid)

        path = str(_cfg.ROXY_OPEN_PATH).format(profile_id=pid)
        params = dict(getattr(_cfg, "ROXY_OPEN_EXTRA_PARAMS", {}) or {})
        # Roxy 官方 /browser/open body: {workspaceId, dirId, args, forceOpen, headless}
        params.setdefault("workspaceId", _workspace_id_value())
        params.setdefault("dirId", int(pid) if str(pid).isdigit() else pid)
        params.setdefault("args", [])
        params.setdefault("forceOpen", True)
        # ROXY_OPEN_HEADLESS 是显式开关，优先级应高于 ROXY_OPEN_EXTRA_PARAMS，
        # 否则 extra 里残留 headless=False 会导致 WebUI 保存无头后仍弹窗口。
        params["headless"] = bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False))
        logger.info("[Roxy] open 参数：profile=%s headless=%s keep_open=%s", pid, params.get("headless"), getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        result = self.request(
            _cfg.ROXY_OPEN_METHOD,
            path,
            params=params if _cfg.ROXY_OPEN_METHOD.upper() == "GET" else None,
            json_body=params if _cfg.ROXY_OPEN_METHOD.upper() != "GET" else None,
        )
        debugger_address = self._extract_debugger_address(result)
        logger.info("[Roxy] open 返回摘要: debugger=%s raw=%s", debugger_address, json.dumps(result, ensure_ascii=False)[:800])
        webdriver_url = _first(result, [
            ("webdriver",), ("webDriver",), ("webdriver_url",), ("webdriverUrl",),
            ("selenium",), ("selenium_url",), ("seleniumUrl",),
            ("data", "webdriver"), ("data", "webDriver"), ("data", "webdriver_url"), ("data", "webdriverUrl"),
            ("data", "selenium"), ("data", "selenium_url"), ("data", "seleniumUrl"),
        ]) or None
        ws_endpoint = _first(result, [
            ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
            ("data", "ws"), ("data", "wsEndpoint"), ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
        ]) or None
        if not debugger_address and not webdriver_url:
            raise RuntimeError(f"Roxy 已打开环境但未返回 Selenium/调试地址，请检查 ROXY_OPEN_PATH 或接口响应: {result}")
        return RoxyOpenResult(
            pid,
            result,
            debugger_address=debugger_address,
            webdriver_url=webdriver_url,
            ws_endpoint=ws_endpoint,
            created_by_run=created_by_run,
        )

    def close_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        path = str(_cfg.ROXY_CLOSE_PATH).format(profile_id=profile_id)
        try:
            body = {
                "workspaceId": _workspace_id_value(),
                "dirId": int(profile_id) if str(profile_id).isdigit() else profile_id,
            }
            self.request(
                _cfg.ROXY_CLOSE_METHOD,
                path,
                params=body if str(_cfg.ROXY_CLOSE_METHOD).upper() == "GET" else None,
                json_body=body if str(_cfg.ROXY_CLOSE_METHOD).upper() != "GET" else None,
            )
            logger.info("[Roxy] 已关闭环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 关闭环境失败：%s", exc)

    def delete_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        path = str(getattr(_cfg, "ROXY_DELETE_PATH", "/browser/delete")).format(profile_id=profile_id)
        method = str(getattr(_cfg, "ROXY_DELETE_METHOD", "POST") or "POST")
        try:
            body = {
                "workspaceId": _workspace_id_value(),
                "dirIds": [int(profile_id) if str(profile_id).isdigit() else profile_id],
            }
            self.request(
                method,
                path,
                params=body if method.upper() == "GET" else None,
                json_body=body if method.upper() != "GET" else None,
            )
            logger.info("[Roxy] 已删除环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 删除环境失败：%s", exc)

    def cleanup_profile(self, opened: RoxyOpenResult | None) -> None:
        """任务结束清理：关闭窗口；一号一环境时删除本轮创建的 Profile。"""
        if not opened or not opened.profile_id:
            return
        keep_open = bool(getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        if not keep_open:
            self.close_profile(opened.profile_id)

        should_delete = (
            bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
            and bool(getattr(_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True))
            and bool(opened.created_by_run)
        )
        if should_delete:
            # 删除前尽量确保已关闭；若 keep_open=True 则不删除，便于调试保留现场。
            if keep_open:
                logger.info("[Roxy] ROXY_KEEP_BROWSER_OPEN=True，跳过删除环境：%s", opened.profile_id)
                return
            self.delete_profile(opened.profile_id)

    @staticmethod
    def _extract_debugger_address(payload: dict) -> str | None:
        value = _first(payload, [
            ("debuggerAddress",), ("debugger_address",), ("debugAddress",),
            ("debuggingPortUrl",), ("debugging_port_url",),
            ("remoteDebuggingAddress",), ("remote_debugging_address",),
            ("http",), ("debugHttp",), ("debug_http",),
            ("data", "debuggerAddress"), ("data", "debugger_address"), ("data", "debugAddress"),
            ("data", "debuggingPortUrl"), ("data", "debugging_port_url"),
            ("data", "remoteDebuggingAddress"), ("data", "remote_debugging_address"),
            ("data", "http"), ("data", "debugHttp"), ("data", "debug_http"),
        ])
        if value:
            value = value.strip()
            # 兼容 http://127.0.0.1:xxxx / 127.0.0.1:xxxx / :xxxx / 9222
            value = value.replace("http://", "").replace("https://", "").strip("/")
            if value.startswith(":") and value[1:].isdigit():
                return f"127.0.0.1{value}"
            if value.isdigit():
                return f"127.0.0.1:{value}"
            if ":" in value and not value.startswith(":"):
                return value
        port = _first(payload, [
            ("debuggingPort",), ("debugging_port",), ("debug_port",), ("port",),
            ("data", "debuggingPort"), ("data", "debugging_port"), ("data", "debug_port"), ("data", "port"),
        ])
        if port:
            port = str(port).strip()
            if port.startswith(":"):
                port = port[1:]
            if port.isdigit():
                return f"127.0.0.1:{port}"
        return None
