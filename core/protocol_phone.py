# -*- coding: utf-8 -*-
"""注册链路的可选 add-phone 分支，复用原有 SMS 供应商，不引入浏览器。"""
import json
import logging
import re
import time

from config import register as settings, codex as sms_settings
from core import openai_auth as auth, sms_provider
from core.protocol_registration import step_info, landing_step
from core.registration_service import StopRequested

logger = logging.getLogger(__name__)
_AUTH = 'https://auth.openai.com'
# 只有明确的号码拒绝才换号；会话错误/CF/频控/未知结果不增加取号次数。
_NUMBER_REJECTED = {
    'phone_number_already_in_use', 'already_in_use', 'already_taken',
    'phone_already_verified', 'already_verified', 'disallowed_phone',
    'invalid_phone_number', 'phone_number_invalid', 'blocked_phone', 'phone_number_blocked',
}
_WRONG_CODE = {'wrong_phone_otp_code', 'invalid_phone_otp_code', 'expired_phone_otp_code'}


def _post(session, path, body, referer):
    auth._check_stop_requested()
    headers = session.get_auth_headers(referer=_AUTH + referer)
    headers.pop('origin', None)
    kwargs = {} if body is None else {'data': json.dumps(body)}
    response = session.post(_AUTH + '/api/accounts/' + path, headers=headers, **kwargs)
    if body is None and response.status_code == 200:
        try:
            response.json()
        except ValueError:
            if not (response.text or '').strip():
                return {}
    return auth.parse_auth_step_response(response, path)


def _cancel(activation_id, http):
    try:
        sms_provider.cancel(activation_id, http)
    except Exception:
        logger.debug('[注册手机验证] 取消号码失败', exc_info=True)


def _wait_and_validate(session, activation_id, http) -> dict:
    """同号有限重发/错码恢复；过滤已经提交过的码，不重置单号截止时间。"""
    timeout = max(1, int(sms_settings.SMS_CODE_WAIT))
    started = time.monotonic()
    deadline = started + timeout
    interval = max(1, int(settings.PROTOCOL_PHONE_RESEND_INTERVAL))
    max_resends = max(0, min(10, int(settings.PROTOCOL_PHONE_MAX_RESENDS)))
    code_attempts = max(1, min(10, int(settings.PROTOCOL_PHONE_CODE_ATTEMPTS)))
    sent_resends = 0
    last_resend_at = started
    seen_codes = set()

    def on_poll(_elapsed):
        nonlocal sent_resends, last_resend_at
        auth._check_stop_requested()
        now = time.monotonic()
        if sent_resends < max_resends and now < deadline and now - last_resend_at >= interval:
            # 先记尝试次数：超时等未知结果不重新发送同一次请求。
            sent_resends += 1
            last_resend_at = now
            _post(session, 'phone-otp/resend', None, '/phone-verification')
            sms_provider.set_status(activation_id, 3, http=http)
            logger.info('[注册手机验证] 已请求同号重发短信 %s/%s', sent_resends, max_resends)

    for attempt in range(code_attempts):
        auth._check_stop_requested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise sms_provider.SmsCodeTimeout('单号等待短信超时')
        code = sms_provider.wait_for_sms_code(
            activation_id, http, max_wait=remaining, on_poll=on_poll, exclude_codes=seen_codes)
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
            raise RuntimeError('接码平台未返回有效的六位短信验证码')
        if code in seen_codes:
            raise RuntimeError('接码平台重复返回已经提交过的短信验证码')
        seen_codes.add(code)
        try:
            return _post(session, 'phone-otp/validate', {'code': code}, '/phone-verification')
        except Exception as exc:
            response = getattr(exc, 'response', None)
            status = getattr(response, 'status_code', None)
            error_code = auth._extract_error_code(response) if response is not None else ''
            if not (status in (200, 400, 409, 422) and error_code in _WRONG_CODE):
                raise
            if attempt + 1 >= code_attempts:
                raise
            sms_provider.set_status(activation_id, 3, http=http)
            logger.info('[注册手机验证] 当前验证码无效，继续在同号等待新验证码')
    raise RuntimeError('手机验证码重试次数耗尽')


def verify_registration_phone(session) -> dict:
    """仅显式开启才取号；号码拒绝/收码超时可换号，其它错误保持原样。"""
    if not settings.PROTOCOL_REGISTRATION_PHONE_ENABLED:
        raise RuntimeError('注册需要手机验证；请在协议配置启用纯协议注册手机验证并配置接码平台')
    auth._check_stop_requested()
    # 配置上界避免错误配置使一个 worker 无限取号。
    attempts = max(1, min(20, int(sms_settings.SMS_MAX_RETRIES)))
    http = sms_provider._http()
    try:
        for attempt in range(1, attempts + 1):
            activation_id = None
            verified = False
            retry = False
            try:
                auth._check_stop_requested()
                activation_id, phone = sms_provider.acquire_number(http)
                phone = str(phone).strip().lstrip('+')
                if not re.fullmatch(r'[1-9][0-9]{5,14}', phone):
                    raise RuntimeError('接码平台未返回有效的国际手机号')
                logger.info('[注册手机验证] 第 %s/%s 次取号，验证码已隐藏', attempt, attempts)
                sent = _post(session, 'add-phone/send', {'phone_number': '+' + phone}, '/add-phone')
                page, url = step_info(sent)
                # 有些已验证号码直接返回下一个业务步骤；不能继续等不存在的短信。
                if url and landing_step(url) in ('external_url', 'about_you'):
                    verified = True
                    result = sent
                else:
                    if page != 'phone_otp_verification' and not (
                        url and url.split('?', 1)[0].rstrip('/') == _AUTH + '/phone-verification'
                    ):
                        raise RuntimeError('add-phone/send 未返回手机验证码状态')
                    sms_provider.set_status(activation_id, 1, http=http)
                    result = _wait_and_validate(session, activation_id, http)
                    page, url = step_info(result)
                    if not url or landing_step(url) not in ('external_url', 'about_you', 'mfa_challenge'):
                        raise RuntimeError('手机验证码验证后缺少可继续的认证状态')
                    verified = True
                # 远端已经验证成功，平台记账失败不能重新提交或重新取号。
                try:
                    sms_provider.complete(activation_id, http)
                except Exception:
                    logger.warning('[注册手机验证] 已验证成功，接码平台完成状态回写失败')
                return result
            except StopRequested:
                raise
            except sms_provider.SmsNoBalanceError:
                raise
            except (sms_provider.SmsCodeTimeout, sms_provider.SmsNoNumbersError):
                retry = True
                if attempt == attempts:
                    raise
            except Exception as exc:
                response = getattr(exc, 'response', None)
                status = getattr(response, 'status_code', None)
                code = auth._extract_error_code(response) if response is not None else ''
                retry = status in (200, 400, 409, 422) and code in (_NUMBER_REJECTED | _WRONG_CODE)
                if not retry or attempt == attempts:
                    raise
            finally:
                if activation_id and not verified:
                    _cancel(activation_id, http)
            if retry:
                auth._interruptible_sleep(3.0)
        raise RuntimeError('注册手机验证重试次数耗尽')
    finally:
        http.close()
