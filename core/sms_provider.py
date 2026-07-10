# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - L：本地 JSON 管理接口，文档 L_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活。
"""
import json
import logging
import threading
import time
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

# GrizzlySMS 规则：号码取出后 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbersError(SmsProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsCodeTimeout(SmsProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


def _http() -> CurlSession:
    s = CurlSession(impersonate=IMPERSONATE)
    s.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return s


def _provider() -> str:
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _request_grizzly(http: CurlSession, params: dict) -> str:
    """
    发一个 GrizzlySMS API 请求，返回去空白的响应文本。
    统一识别公共错误码并抛对应异常。
    """
    base_params = {"api_key": _cfg.SMS_API_KEY}
    base_params.update(params)
    resp = http.get(_cfg.SMS_API_BASE, params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"GrizzlySMS HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsProviderError("接码平台 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError("接码平台暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsProviderError("接码平台地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsProviderError(f"接码平台请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsProviderError("激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"该服务被平台禁售：{text}")

    return text


def _l_url(path: str) -> str:
    base = str(getattr(_cfg, "L_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers() -> dict:
    token = str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_l_url(path), headers=_l_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_l_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "L_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


# ============================================================
# 取号
# ============================================================

def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            payload = {
                "service": service or _cfg.SMS_SERVICE,
                "country": country or _cfg.SMS_COUNTRY,
            }
            if _cfg.SMS_MAX_PRICE:
                payload["maxPrice"] = _cfg.SMS_MAX_PRICE

            data = _post_l_json(http, "/api/admin/l/take-phone", payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "L_PHONE_PREFIX", "") or "")
            phone = _normalize_l_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return activation_id, phone

        params = {
            "action": "getNumber",
            "service": service or _cfg.SMS_SERVICE,
            "country": country or _cfg.SMS_COUNTRY,
        }
        if _cfg.SMS_MAX_PRICE:
            params["maxPrice"] = _cfg.SMS_MAX_PRICE

        text = _request_grizzly(http, params)
        # 成功格式：ACCESS_NUMBER:激活ID:号码
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsProviderError(f"getNumber 非预期响应：{text[:200]}")
        parts = text.split(":")
        if len(parts) < 3:
            raise SmsProviderError(f"getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = parts[2].strip()
        _ACQUIRED_AT[activation_id] = time.time()
        logger.info(f"[SMS] 取号成功：activation_id={activation_id}, phone=+{phone}")
        return activation_id, phone
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    own_http = http is None
    http = http or _http()
    deadline = time.time() + (max_wait or _cfg.SMS_CODE_WAIT)
    interval = poll_interval or _cfg.SMS_POLL_INTERVAL
    try:
        provider = _provider()
        total_wait = max_wait or _cfg.SMS_CODE_WAIT
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.time())))
            remaining_before = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            text = _request_grizzly(http, {"action": "getStatus", "id": activation_id})

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info(f"[SMS] 第 {round_no} 轮收到验证码：{code}")
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError("激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.time()))
            logger.info(f"[SMS] 第 {round_no} 轮未收到验证码，状态={text}，{interval}s 后重试（剩余 {remaining}s）")
            time.sleep(interval)

        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            logger.debug(f"[SMS:L] 忽略状态设置 id={activation_id}, status={status}")
            return "OK"
        return _request_grizzly(http, {"action": "setStatus", "status": str(status), "id": activation_id})
    finally:
        if own_http:
            http.close()


def complete(activation_id: str, http: CurlSession | None = None) -> None:
    """标记激活完成（status=6）。失败只告警不抛，避免影响主流程。"""
    if _provider() == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    try:
        set_status(activation_id, 6, http=http)
        logger.info(f"[SMS] 已标记完成 activation_id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")


def _do_cancel_sync(activation_id: str, http_factory) -> None:
    """实际的同步取消逻辑：等够 2 分钟限制 → 发请求 → 失败重试一次。"""
    acquired_at = _ACQUIRED_AT.get(activation_id)
    if acquired_at is not None:
        elapsed = time.time() - acquired_at
        if elapsed < _MIN_CANCEL_DELAY:
            wait = _MIN_CANCEL_DELAY - elapsed
            logger.info(
                f"[SMS] 取消等待 GrizzlySMS 2 分钟限制：activation_id={activation_id}，"
                f"还需等 {wait:.0f}s..."
            )
            time.sleep(wait)

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己建一个
    http = http_factory()
    try:
        for attempt in range(1, 3):
            try:
                set_status(activation_id, 8, http=http)
                logger.info(f"[SMS] 已取消 activation_id={activation_id}")
                _ACQUIRED_AT.pop(activation_id, None)
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"[SMS] 取消失败（{exc}），5s 后重试...")
                    time.sleep(5)
                else:
                    logger.warning(
                        f"[SMS] 取消最终失败（不影响结果，需到平台手动取消）：activation_id={activation_id}, {exc}"
                    )
    finally:
        try:
            http.close()
        except Exception:
            pass


def cancel(activation_id: str, http: CurlSession | None = None, background: bool = True) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    失败只告警不抛，不影响主流程。
    """
    if _provider() == "l":
        try:
            _release_l_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return

    if not background:
        _do_cancel_sync(activation_id, _http)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, _http),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")
