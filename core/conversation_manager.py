"""独立 Conversation Pool 文件存储；普通列表仅使用安全摘要。"""
from __future__ import annotations
from pathlib import Path
from core import db

_PATH=Path(__file__).resolve().parent.parent/"会话池.json"
_DEFAULT_MESSAGES=[
 "你好，请用一句话介绍你自己。",
 "请给我三条提高英语学习效率的建议。",
 "用 Python 写一个计算斐波那契数列第 n 项的函数，并简单解释。",
 "请用两句话向小学生解释什么是人工智能。",
 "谢谢，请给我一句坚持下去的鼓励。",
]
def _now(): return db._now()
def _load():
 fresh=not _PATH.exists()
 v=db._read_json(_PATH,{"templates":{},"bindings":{}})
 if fresh and not v.get("templates"):
  now=_now();v["templates"]["default-five"]={"id":"default-five","name":"默认五轮对话","messages":list(_DEFAULT_MESSAGES),"enabled":True,"retry":{},"created_at":now,"updated_at":now};_save(v)
 return v
def _save(v): db._write_json(_PATH,v)
def _key(account_id,template_id): return f"{int(account_id)}:{template_id}"
def put_template(template_id,name,messages,enabled=True,retry=None):
 if not template_id or not name or not isinstance(messages,list) or any(not isinstance(x,str) or not x.strip() for x in messages): raise ValueError("模板必须包含名称与非空消息数组")
 with db._LOCK:
  x=_load(); old=x["templates"].get(template_id,{})
  x["templates"][template_id]={"id":template_id,"name":name,"messages":[s.strip() for s in messages],"enabled":bool(enabled),"retry":retry or {},"created_at":old.get("created_at",_now()),"updated_at":_now()};_save(x)
def list_templates():
 return [{k:v for k,v in x.items() if k!="messages"}|{"message_count":len(x.get("messages",[]))} for x in _load()["templates"].values()]
def get_template(template_id): return _load()["templates"].get(template_id)
def delete_template(template_id):
 with db._LOCK:
  x=_load(); x["templates"].pop(template_id,None); _save(x)
def bind(account_id,template_id):
 with db._LOCK:
  x=_load()
  if template_id not in x["templates"]: raise KeyError(template_id)
  k=_key(account_id,template_id); row=x["bindings"].get(k)
  if row:return dict(row)
  row={"account_id":int(account_id),"template_id":template_id,"conversation_id":"","status":"queued","current_index":0,"attempt":0,"last_error":"","stage":"","http_status":None,"reason":"","created_at":_now(),"updated_at":_now()};x["bindings"][k]=row;_save(x);return dict(row)
def get_binding(account_id,template_id): return _load()["bindings"].get(_key(account_id,template_id))
def list_bindings(account_id=None):
 rows=list(_load()["bindings"].values());return [dict(x) for x in rows if account_id is None or int(x["account_id"])==int(account_id)]
def claim(account_id,template_id,retry=False):
 with db._LOCK:
  x=_load(); row=x["bindings"].get(_key(account_id,template_id))
  if not row or row["status"] in {"completed","running"} or (row["status"] in {"partial","failed","needs_browser_verification"} and not retry): return None
  if retry: row["attempt"]+=1
  row["status"]="running";row["updated_at"]=_now();_save(x);return dict(row)
def retry(account_id,template_id): return claim(account_id,template_id,retry=True)
def checkpoint(account_id,template_id,**fields):
 with db._LOCK:
  x=_load();row=x["bindings"][_key(account_id,template_id)];row.update(fields);row["updated_at"]=_now();_save(x);return dict(row)
def unbind(account_id,template_id):
 with db._LOCK:
  x=_load();k=_key(account_id,template_id);existed=k in x["bindings"];x["bindings"].pop(k,None);_save(x);return existed
