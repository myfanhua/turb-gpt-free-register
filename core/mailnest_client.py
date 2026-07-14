#!/usr/bin/python3.12
# -*- coding: utf-8 -*-
#
# @Time  : 2026/7/14 14:20
# @File  : mailnest_client.py

import time
import logging

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)

EMAILS = set()


class MailNestClientError(RuntimeError):
    """mailnest 邮箱服务相关异常。"""


def __req(method, url, params=None, json=None):
    resp = requests.request(
        method,
        url,
        params=params,
        json=json,
        headers={
            "Authorization": f"Bearer {_email_cfg.MAIL_NEST_API_KEY}",
        },
        verify=False,
    )
    if resp.status_code == 401:
        raise MailNestClientError('mailnest api key 非法')
    resp.raise_for_status()
    resp_json = resp.json()
    if resp_json['code'] != '00000':
        raise MailNestClientError(f'mailnest {resp_json}')
    return resp_json['data']


def get_email():
    api_key = getattr(_email_cfg, 'MAIL_NEST_API_KEY', '')
    if not api_key:
        raise MailNestClientError('MAIL_NEST_API_KEY 未配置，请在 config/email.py 中设置你的 MAIL_NEST_API_KEY')
    project_code = getattr(_email_cfg, 'MAIL_NEST_PROJECT_CODE', '')
    if not api_key:
        raise MailNestClientError(
            'MAIL_NEST_PROJECT_CODE 未配置，请在 config/email.py 中设置你的 MAIL_NEST_PROJECT_CODE')
    email = __req(
        'POST', "https://mailnest.top/api/v1/email/temporary/buy",
        json={
            "project_code": project_code,
            "count": 1,
        }
    )[0]['email']
    logger.info(f'获取到临时邮箱 | email={email} project_code={project_code}')
    EMAILS.add(email)
    return email


def _get_mails(email):
    return __req(
        'POST',
        'https://mailnest.top/api/v1/email/receive',
        json={
            "email": email,
        },
    )


def fetch_latest_otp(email):
    for i in range(30):
        try:
            mails = _get_mails(email)
            for mail in mails:
                if code := mail['code_match']:
                    return code
        except Exception:
            pass
        time.sleep(3)
    raise MailNestClientError(f"等待验证码超时")


def get_account_context(email):
    return email in EMAILS
