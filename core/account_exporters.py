# -*- coding: utf-8 -*-
"""仅用于已保存账号的 Sub2API / Cockpit 离线导出。"""
from __future__ import annotations
import base64, json, os
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EXPORT_DIR = _ROOT / "exports"

def build_auth_artifacts(access_token, *, registration_password=None, session_data=None, cookies=None):
    out = {"access_token": access_token}
    if registration_password: out["registration_password"] = registration_password
    if session_data: out["session"] = session_data
    if cookies: out["cookies"] = cookies
    return out

def _jwt_exp(token):
    try:
        part = str(token).split(".")[1]; part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part)).get("exp"))
    except Exception: return None

def normalize_account(row):
    extra = row.get("extra_json") or {}
    if isinstance(extra, str):
        try: extra = json.loads(extra)
        except Exception: extra = {}
    artifacts = extra.get("auth_artifacts") if isinstance(extra, dict) else {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    access = row.get("access_token") or artifacts.get("access_token") or ""
    # Outlook 邮箱素材的 refresh_token 不是 OpenAI OAuth refresh_token，绝不能据此
    # 宣称 ChatGPT access token 可自动刷新。只有显式 OAuth 字段才允许导出为此用途。
    refresh = artifacts.get("refresh_token") or row.get("oauth_refresh_token") or ""
    identity = row.get("id_token") or artifacts.get("id_token") or ""
    missing = [name for name, value in (("access_token", access), ("oauth_refresh_token", refresh), ("id_token", identity), ("session", artifacts.get("session")), ("cookies", artifacts.get("cookies"))) if not value]
    return {"account_id": str(row.get("user_id") or row.get("account_id") or row.get("id") or ""), "email": row.get("email") or "", "access_token": access, "refresh_token": refresh, "id_token": identity, "expires_at": _jwt_exp(access), "registration_password": artifacts.get("registration_password") or row.get("registration_password") or "", "session": artifacts.get("session"), "cookies": artifacts.get("cookies"), "note": row.get("note") or "", "missing": missing, "refreshable": bool(refresh), "session_renewal_possible": bool(artifacts.get("cookies"))}

class _BaseExporter:
    format_name = ""
    def __init__(self, export_dir=None): self.export_dir = Path(export_dir or _EXPORT_DIR)
    def _write(self, data):
        self.export_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self.format_name}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
        path = self.export_dir / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try: os.chmod(path, 0o600)
        except OSError: pass
        return path, {"accounts": len(data.get("accounts", [])) if isinstance(data, dict) else len(data), "missing": sum(len(normalize_account(r)["missing"]) for r in self._source_rows)}

class Sub2APIExporter(_BaseExporter):
    format_name = "sub2api"
    def build(self, rows):
        accounts=[]
        for row in rows:
            a=normalize_account(row)
            accounts.append({"platform":"openai","type":"oauth","expires_at":a["expires_at"],"auto_pause_on_expired":True,"concurrency":10,"priority":1,"credentials":{"access_token":a["access_token"],"refresh_token":a["refresh_token"],"id_token":a["id_token"]},"extra":{"email":a["email"],"account_id":a["account_id"],"session":a["session"],"cookies":a["cookies"],"missing_credentials":a["missing"],"refreshable":a["refreshable"],"session_renewal_possible":a["session_renewal_possible"],"snapshot_only":not a["refreshable"]}})
        return {"exported_at":datetime.now().isoformat(timespec="seconds"),"proxies":[],"accounts":accounts}
    def export(self, rows): self._source_rows=list(rows); return self._write(self.build(self._source_rows))

class CockpitExporter(_BaseExporter):
    format_name = "cockpit"
    def build(self, rows, single=False):
        out=[]
        for row in rows:
            a=normalize_account(row); item={"type":"codex","id_token":a["id_token"],"access_token":a["access_token"],"refresh_token":a["refresh_token"],"account_id":a["account_id"],"snapshot_exported_at":datetime.now().isoformat(timespec="seconds"),"email":a["email"],"expired":a["expires_at"],"expires_at":a["expires_at"],"missing_credentials":a["missing"],"refreshable":a["refreshable"],"session_renewal_possible":a["session_renewal_possible"],"snapshot_only":not a["refreshable"]}
            if a["note"]: item["account_note"]=a["note"]
            out.append(item)
        return out[0] if single and len(out)==1 else out
    def export(self, rows): self._source_rows=list(rows); return self._write(self.build(self._source_rows))

class FullAccountAssetExporter(_BaseExporter):
    """受控下载的完整资产 JSON；调用方绝不将内容嵌入 API 响应。"""
    format_name = "full-assets"
    def build(self, rows):
        accounts=[]; missing_total=[]
        for row in rows:
            a=normalize_account(row); extra=row.get("extra_json") or {}
            if isinstance(extra,str):
                try: extra=json.loads(extra)
                except Exception: extra={}
            asset=extra.get("email_asset") if isinstance(extra,dict) else None
            missing=list(a["missing"])
            if not asset: missing.append("email_asset")
            accounts.append({"account_id":a["account_id"],"email":a["email"],"auth_artifacts":{"access_token":a["access_token"],"refresh_token":a["refresh_token"],"id_token":a["id_token"],"registration_password":a["registration_password"],"session":a["session"],"cookies":a["cookies"]},"email_asset":asset or {"provider":row.get("email_source") or "","email_address":a["email"],"exportable":False},"missing_fields":missing})
            missing_total.extend(missing)
        return {"exported_at":datetime.now().isoformat(timespec="seconds"),"format":"full_account_assets","accounts":accounts}
    def export(self, rows): self._source_rows=list(rows); return self._write(self.build(self._source_rows))
