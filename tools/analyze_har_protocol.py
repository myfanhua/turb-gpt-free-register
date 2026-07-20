#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Reqorder/HAR JSON 抽取纯协议链路、指纹 p 数组、JS 入口与接口清单。"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import pathlib
import urllib.parse

SENSITIVE_KEYS = {"authorization", "cookie", "set-cookie", "openai-sentinel-token", "openai-sentinel-so-token"}


def decode_sentinel_p(value: str):
    if not value:
        return None
    text = value
    for prefix in ("gAAAAAC", "gAAAAAB"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("~", 1)[0]
    try:
        return json.loads(base64.b64decode(text).decode("utf-8"))
    except Exception:
        return None


def short_body(text: str, limit: int = 260):
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k in {"p", "prepare_token", "token", "code"}:
                    out[k] = f"<{k}:len={len(str(v))}>"
                elif k in {"batch", "series"}:
                    out[k] = f"<{k}:items={len(v) if isinstance(v, list) else '?'}>"
                else:
                    out[k] = v
            return out
    except Exception:
        pass
    return text[:limit]


def header_dict(headers):
    out = {}
    for h in headers or []:
        k = h.get("name", "")
        v = h.get("value", "")
        if k.lower() in SENSITIVE_KEYS:
            out[k] = f"<redacted:len={len(v)}>"
        else:
            out[k] = v
    return out


def classify(url: str) -> str:
    u = urllib.parse.urlparse(url)
    path = u.path
    host = u.netloc
    if host == "browser-intake-datadoghq.com":
        return "datadog-rum"
    if "/ces/v1/rgstr" in path or "/ces/v1/" in path or host == "ab.chatgpt.com":
        return "frontend-telemetry"
    if "/api/auth/" in path:
        return "nextauth-oauth"
    if "auth.openai.com" in host:
        return "openai-auth"
    if "/sentinel/chat-requirements/" in path:
        return "chatgpt-sentinel"
    if "/backend-anon/" in path:
        return "chatgpt-anon-bootstrap"
    if "/backend-api/" in path:
        return "chatgpt-auth-bootstrap"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har")
    ap.add_argument("-o", "--output", default="docs/protocol_har_summary.json")
    args = ap.parse_args()
    har_path = pathlib.Path(args.har)
    data = json.loads(har_path.read_text(encoding="utf-8"))
    entries = data.get("log", {}).get("entries", [])

    summary = {
        "source": str(har_path),
        "entry_count": len(entries),
        "domains": collections.Counter(urllib.parse.urlparse(e["request"]["url"]).netloc for e in entries),
        "classes": collections.Counter(classify(e["request"]["url"]) for e in entries),
        "requests": [],
        "fingerprints": [],
        "js_entrypoints": [],
    }

    js_seen = set()
    for i, e in enumerate(entries):
        req = e.get("request", {})
        resp = e.get("response", {})
        url = req.get("url", "")
        post_text = (req.get("postData") or {}).get("text") or ""
        item = {
            "index": i,
            "class": classify(url),
            "method": req.get("method"),
            "status": resp.get("status"),
            "url": url,
            "request_headers": header_dict(req.get("headers")),
            "post": short_body(post_text),
            "response_mime": (resp.get("content") or {}).get("mimeType"),
            "response_size": len((resp.get("content") or {}).get("text") or ""),
        }
        summary["requests"].append(item)

        # body.p
        try:
            body = json.loads(post_text) if post_text else None
        except Exception:
            body = None
        if isinstance(body, dict) and isinstance(body.get("p"), str):
            arr = decode_sentinel_p(body["p"])
            if isinstance(arr, list):
                summary["fingerprints"].append({"index": i, "source": "body.p", "url": url, "array": arr})
                if len(arr) > 5 and isinstance(arr[5], str) and arr[5] not in js_seen:
                    js_seen.add(arr[5]); summary["js_entrypoints"].append(arr[5])

        for h in req.get("headers") or []:
            if h.get("name", "").lower() == "openai-sentinel-token":
                try:
                    token = json.loads(h.get("value") or "{}")
                except Exception:
                    token = {}
                arr = decode_sentinel_p(token.get("p", ""))
                if isinstance(arr, list):
                    summary["fingerprints"].append({"index": i, "source": "openai-sentinel-token.p", "url": url, "flow": token.get("flow"), "array": arr})
                    if len(arr) > 5 and isinstance(arr[5], str) and arr[5] not in js_seen:
                        js_seen.add(arr[5]); summary["js_entrypoints"].append(arr[5])

    # Counter 转普通 dict
    summary["domains"] = dict(summary["domains"].most_common())
    summary["classes"] = dict(summary["classes"].most_common())

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已输出 {out}，请求 {len(summary['requests'])} 条，指纹 {len(summary['fingerprints'])} 组，JS 入口 {len(summary['js_entrypoints'])} 个")


if __name__ == "__main__":
    main()
