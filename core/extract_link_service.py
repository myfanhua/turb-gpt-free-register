# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db
from core.kakao_extract_link_provider import (
    build_kakao_batches,
    KakaoBatchPlan,
    KakaoExtractLinkClient,
    map_kakao_results,
)

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
SUPPORTED_PROVIDERS = {"legacy", "kakao_batch"}


def provider_name(value: str | None = None) -> str:
    raw = str(value or _runtime_setting("EXTRACT_LINK_PROVIDER", "legacy") or "legacy").strip().lower()
    aliases = {
        "legacy": "legacy",
        "old": "legacy",
        "sse": "legacy",
        "kakao": "kakao_batch",
        "kakao_batch": "kakao_batch",
        "kakao-batch": "kakao_batch",
    }
    provider = aliases.get(raw)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("提链 provider 无效，仅支持 legacy / kakao_batch")
    return provider


def kakao_batch_size(value: int | str | None = None) -> int:
    raw = value if value is not None else _runtime_setting("KAKAO_EXTRACT_BATCH_SIZE", 5)
    try:
        size = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Kakao 每批数量必须为 1-5") from exc
    if not 1 <= size <= 5:
        raise ValueError("Kakao 每批数量必须为 1-5")
    return size


def _link_type(value: str | None = None) -> str:
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal")
    return t


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return base


def _cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


def _kakao_cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("KAKAO_EXTRACT_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("KAKAO_EXTRACT_CDK/CDK 为空")
    return cdk


def _make_kakao_client(*, cdk: str | None = None) -> KakaoExtractLinkClient:
    return KakaoExtractLinkClient(
        api_base=str(_runtime_setting("KAKAO_EXTRACT_API_BASE", "https://tiqu.dxmcs.xin") or "").strip(),
        cdk=_kakao_cdk(cdk),
        timeout_seconds=_int_setting("KAKAO_EXTRACT_TIMEOUT_SECONDS", 930, 30, 1200),
        poll_interval=_float_setting("KAKAO_EXTRACT_POLL_INTERVAL", 4.0, 0.5, 30.0),
        request_timeout=_int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300),
    )


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def latest_kakao_remaining_count() -> int | None:
    """返回最近一次 Kakao 批次写入的剩余次数。"""
    try:
        rows = db.list_accounts(limit=5000, archived="all")
    except Exception:
        return None
    candidates = [
        row for row in rows
        if str(row.get("extract_link_provider") or "").strip().lower() == "kakao_batch"
        and row.get("extract_link_cdk_remaining") is not None
    ]
    candidates.sort(
        key=lambda row: str(
            row.get("extract_link_checked_at")
            or row.get("extract_link_completed_at")
            or row.get("updated_at")
            or ""
        ),
        reverse=True,
    )
    if not candidates:
        return None
    try:
        return int(candidates[0].get("extract_link_cdk_remaining"))
    except (TypeError, ValueError):
        return None


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def query_cdk(*, cdk: str | None = None) -> dict:
    base = _api_base()
    code = _cdk(cdk)
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session()
    try:
        if s is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = s.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_extract_job(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "token": token}
    s = _session()
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/extract",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict) or not data.get("job_id"):
                raise RuntimeError(f"提链服务未返回 job_id: {data}")
            return data
        resp = s.post(f"{base}/api/extract", json=payload, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _iter_sse_events(*, job_id: str, cdk: str):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': _cdk(cdk)})}"
    s = _session()
    try:
        if s is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with urlopen(req, timeout=timeout) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"raw": text}
                            yield event, data
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
            return
        resp = s.get(url, timeout=timeout, stream=True)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        event = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines():
            if raw is None:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _extract_error_message(data) -> str:
    """尽量从提链服务返回的任意错误结构中提取用户可读原因。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _run_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str) -> dict:
    logs: list[str] = []
    last_event = None
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_extract_job(token=access_token, link_type=link_type, cdk=cdk)
        job_id = str(job.get("job_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
            "cdk_remaining": job.get("cdk_remaining"),
        })
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs}
                db.update_account_extract(account_id, final)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                msg = _extract_error_message(data)
                raise RuntimeError(msg or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual", link_type: str | None = None, cdk: str | None = None) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        lt = _link_type(link_type)
        code = _cdk(cdk)
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(_run_extract, account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger)
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt}
    except Exception:
        _QUEUE_SLOTS.release()
        raise


def _account_response_item(account: dict, **extra) -> dict:
    item = {
        "id": int(account.get("id") or account.get("account_id") or 0),
        "email": str(account.get("email") or ""),
    }
    item.update(extra)
    return item


def _run_kakao_batch(
    *,
    plan: KakaoBatchPlan,
    client: KakaoExtractLinkClient,
    trigger: str,
    existing_batch_id: str | None = None,
) -> dict:
    batch_id = str(existing_batch_id or "").strip()
    account_ids = [
        account_id
        for ids in plan.account_ids_by_result_index.values()
        for account_id in ids
    ]

    def persist_partial_successes(snapshot) -> None:
        """轮询中一旦出现 paymentLink 就立即落库。

        上游偶尔会在批次结束附近快速清理 batchId；若只等最终
        done 响应，已经生成的链接也会跟着 404 一起丢失。
        """
        partial = map_kakao_results(plan, getattr(snapshot, "results", []))
        for result_index, ids in plan.account_ids_by_result_index.items():
            for account_id in ids:
                one = dict(partial.get(account_id) or {})
                if one.get("status") != "success" or not one.get("ok"):
                    continue
                one.update({
                    "provider": "kakao_batch",
                    "batch_id": batch_id,
                    "batch_number": plan.batch_number,
                    "batch_total": plan.batch_total,
                    "result_index": result_index,
                    "charged_count": getattr(snapshot, "charged_count", 0),
                    "cdk_remaining": getattr(snapshot, "remaining_count", None),
                    "message": "Kakao 提链成功（轮询中已保存）",
                })
                db.update_account_extract(account_id, one)

    try:
        if not batch_id:
            accepted = client.submit(plan.tokens)
            batch_id = accepted.batch_id
        for result_index, ids in plan.account_ids_by_result_index.items():
            for account_id in ids:
                db.mark_account_extract_running(account_id)
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "running",
                    "provider": "kakao_batch",
                    "batch_id": batch_id,
                    "batch_number": plan.batch_number,
                    "batch_total": plan.batch_total,
                    "result_index": result_index,
                    "link_type": "kakao_pay",
                    "message": f"Kakao 第 {plan.batch_number}/{plan.batch_total} 批处理中",
                })

        completed = client.poll(batch_id, on_update=persist_partial_successes)
        mapped = map_kakao_results(plan, completed.results)
        for result_index, ids in plan.account_ids_by_result_index.items():
            for account_id in ids:
                final = dict(mapped.get(account_id) or {
                    "ok": False,
                    "status": "failed",
                    "error": "Kakao 服务未返回该账号结果",
                    "message": "Kakao 服务未返回该账号结果",
                })
                final.update({
                    "provider": "kakao_batch",
                    "batch_id": batch_id,
                    "batch_number": plan.batch_number,
                    "batch_total": plan.batch_total,
                    "result_index": result_index,
                    "charged_count": completed.charged_count,
                    "cdk_remaining": completed.remaining_count,
                })
                db.update_account_extract(account_id, final)
        logger.info(
            "[提链] Kakao 批次完成: batch=%s accounts=%s success=%s failed=%s",
            batch_id,
            len(account_ids),
            completed.success_count,
            completed.failure_count,
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "account_count": len(account_ids),
        }
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)}"[:500]
        for result_index, ids in plan.account_ids_by_result_index.items():
            for account_id in ids:
                current = db.get_account(account_id) or {}
                if (
                    str(current.get("extract_link_status") or "").lower() == "success"
                    and bool(
                        current.get("extract_link_long_url")
                        or current.get("extract_link_copy_paste")
                    )
                ):
                    continue
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "failed",
                    "provider": "kakao_batch",
                    "batch_id": batch_id or None,
                    "batch_number": plan.batch_number,
                    "batch_total": plan.batch_total,
                    "result_index": result_index,
                    "error": reason,
                    "message": reason,
                })
        logger.exception("[提链] Kakao 批次失败: batch=%s trigger=%s", batch_id or "unsubmitted", trigger)
        return {"ok": False, "batch_id": batch_id, "error": reason}
    finally:
        _QUEUE_SLOTS.release()


def _enqueue_legacy_accounts(
    *,
    accounts: list[dict],
    trigger: str,
    link_type: str | None,
    cdk: str | None,
) -> dict:
    started, busy, failed = [], [], []
    for account in accounts:
        try:
            queued = enqueue_account_extract(
                account_id=int(account.get("id") or account.get("account_id") or 0),
                email=str(account.get("email") or ""),
                access_token=str(account.get("access_token") or ""),
                trigger=trigger,
                link_type=link_type,
                cdk=cdk,
            )
        except Exception as exc:
            failed.append(_account_response_item(account, error=f"{type(exc).__name__}: {exc}"))
            continue
        item = _account_response_item(
            account,
            **{key: value for key, value in queued.items() if key != "future"},
        )
        if queued.get("accepted"):
            started.append(item)
        elif queued.get("busy"):
            busy.append(item)
        else:
            failed.append(item)
    return {
        "provider": "legacy",
        "batch_count": len(started),
        "started": started,
        "started_count": len(started),
        "busy": busy,
        "busy_count": len(busy),
        "failed": failed,
        "failed_count": len(failed),
    }


def enqueue_accounts_extract(
    *,
    accounts: list[dict],
    trigger: str = "manual_bulk",
    provider: str | None = None,
    batch_size: int | str | None = None,
    link_type: str | None = None,
    cdk: str | None = None,
) -> dict:
    selected_provider = provider_name(provider)
    if selected_provider == "legacy":
        return _enqueue_legacy_accounts(
            accounts=accounts,
            trigger=trigger,
            link_type=link_type,
            cdk=cdk,
        )

    size = kakao_batch_size(batch_size)
    claimed_accounts: list[dict] = []
    busy: list[dict] = []
    failed: list[dict] = []
    for account in accounts:
        account_id = int(account.get("id") or account.get("account_id") or 0)
        if db.claim_account_extract(
            account_id,
            trigger=trigger,
            link_type="kakao_pay",
            provider="kakao_batch",
        ):
            claimed_accounts.append({
                "id": account_id,
                "account_id": account_id,
                "email": str(account.get("email") or ""),
                "access_token": str(account.get("access_token") or ""),
            })
        else:
            busy.append(_account_response_item(account, busy=True, error="该账号正在提链中"))

    plans = build_kakao_batches(claimed_accounts, size) if claimed_accounts else []
    account_by_id = {int(account["id"]): account for account in claimed_accounts}
    started: list[dict] = []
    scheduled_batches = 0
    for plan in plans:
        ids = [
            account_id
            for group in plan.account_ids_by_result_index.values()
            for account_id in group
        ]
        for result_index, result_ids in plan.account_ids_by_result_index.items():
            for account_id in result_ids:
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "queued",
                    "provider": "kakao_batch",
                    "batch_number": plan.batch_number,
                    "batch_total": plan.batch_total,
                    "result_index": result_index,
                    "link_type": "kakao_pay",
                    "message": f"Kakao 第 {plan.batch_number}/{plan.batch_total} 批已入队",
                })
        if not _QUEUE_SLOTS.acquire(blocking=False):
            db.release_account_extract_claims(ids, error="提链队列已满")
            failed.extend(
                _account_response_item(account_by_id[account_id], error="提链队列已满")
                for account_id in ids
            )
            continue
        try:
            client = _make_kakao_client(cdk=cdk)
            _EXECUTOR.submit(
                _run_kakao_batch,
                plan=plan,
                client=client,
                trigger=trigger,
                existing_batch_id=None,
            )
        except Exception as exc:
            _QUEUE_SLOTS.release()
            reason = f"{type(exc).__name__}: {exc}"[:500]
            db.release_account_extract_claims(ids, error=reason)
            failed.extend(
                _account_response_item(account_by_id[account_id], error=reason)
                for account_id in ids
            )
            continue
        scheduled_batches += 1
        started.extend(
            _account_response_item(
                account_by_id[account_id],
                accepted=True,
                busy=False,
                provider="kakao_batch",
                batch_number=plan.batch_number,
                batch_total=plan.batch_total,
            )
            for account_id in ids
        )

    return {
        "provider": "kakao_batch",
        "batch_size": size,
        "batch_count": scheduled_batches,
        "started": started,
        "started_count": len(started),
        "busy": busy,
        "busy_count": len(busy),
        "failed": failed,
        "failed_count": len(failed),
    }


def resume_interrupted_kakao_batches(batches: list[dict]) -> dict:
    resumed = 0
    failed = 0
    for raw in batches or []:
        batch_id = str((raw or {}).get("batch_id") or "").strip()
        accounts = (raw or {}).get("accounts") or []
        mapping: dict[int, list[int]] = {}
        for item in accounts:
            try:
                account_id = int(item.get("account_id"))
                result_index = int(item.get("result_index") or 0)
            except (TypeError, ValueError):
                continue
            mapping.setdefault(result_index, []).append(account_id)
        ids = [account_id for group in mapping.values() for account_id in group]
        if not batch_id or not ids:
            failed += 1
            continue
        if not _QUEUE_SLOTS.acquire(blocking=False):
            for account_id in ids:
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "failed",
                    "error": "恢复 Kakao 批次时提链队列已满，请重新提链",
                    "message": "恢复 Kakao 批次时提链队列已满，请重新提链",
                })
            failed += 1
            continue
        plan = KakaoBatchPlan(
            batch_number=int((raw or {}).get("batch_number") or 1),
            batch_total=int((raw or {}).get("batch_total") or 1),
            tokens=[],
            account_ids_by_result_index=mapping,
        )
        try:
            client = _make_kakao_client()
            _EXECUTOR.submit(
                _run_kakao_batch,
                plan=plan,
                client=client,
                trigger="startup_recovery",
                existing_batch_id=batch_id,
            )
        except Exception as exc:
            _QUEUE_SLOTS.release()
            reason = f"恢复 Kakao 批次失败: {type(exc).__name__}: {exc}"[:500]
            for account_id in ids:
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "failed",
                    "error": reason,
                    "message": reason,
                })
            failed += 1
            continue
        resumed += 1
    return {"resumed_batches": resumed, "failed_batches": failed}
