# -*- coding: utf-8 -*-
"""目标协议 Sentinel JS 的适配层：stdin JSON 输入，stdout JSON 输出。

网络请求仍由 BrowserSession 完成，JS 只在本地 Node VM 中运行会话固定的 SDK。
旧 sentinel/sentinel-runner.js 保留为历史资源，不在注册调用链中回退使用。
"""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from config import openai_protocol as protocol_settings
from config import (
    USER_AGENT, TIMEZONE_IANA, NAVIGATOR_LANGUAGE, NAVIGATOR_LANGUAGES,
    SCREEN_WIDTH, SCREEN_HEIGHT, HARDWARE_CONCURRENCY, DEVICE_MEMORY,
)

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SENTINEL_DIR = _PROJECT_ROOT / 'sentinel'
_RUNNER_PATH = _SENTINEL_DIR / 'openai_sentinel_quickjs.js'
_SDK_PATH = _SENTINEL_DIR / 'sdk.js'
_RUNNER_TIMEOUT = 60


def _resolve_node_executable() -> str:
    return (os.environ.get('NODE_EXECUTABLE') or os.environ.get('OPENAI_SENTINEL_NODE_PATH')
            or ('node.exe' if sys.platform.startswith('win') else 'node'))


def _ensure_runner_environment(sdk_path=None) -> None:
    for path in (_RUNNER_PATH, Path(sdk_path or _SDK_PATH)):
        if not path.is_file():
            raise FileNotFoundError(f'找不到 Sentinel 资源: {path}')


def _run_node(cmd, *, input, cwd, timeout, env):
    """有界、可停止地通信；停止/超时时 kill 并回收子进程和管道。"""
    from core.openai_auth import _check_stop_requested
    _check_stop_requested()
    deadline = time.monotonic() + timeout
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding='utf-8',
                          cwd=cwd, env=env) as proc:
        pending_input = input
        try:
            while True:
                _check_stop_requested()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    stdout, stderr = proc.communicate(input=pending_input, timeout=min(0.25, remaining))
                    _check_stop_requested()
                    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    # communicate 保留上次尚未写完的输入，后续不得重复传入。
                    pending_input = None
        except BaseException:
            if proc.poll() is None:
                proc.kill()
            # 异常路径不再解码输出，避免坏UTF-8掩盖原始停止/超时异常。
            proc.wait()
            raise


def generate_sentinel_token(
    challenge: dict, flow: str, device_id: str,
    user_agent: str | None = None, page_url: str | None = None,
    browser_profile: dict | None = None, sentinel_sid: str | None = None,
    react_listening_key: str | None = None, react_container_key: str | None = None,
    react_resources_key: str | None = None, cookie: str | None = None,
    sdk_path: Path | None = None, action: str = 'token',
) -> str:
    """保持业务接口兼容；目标 JS 使用 requirements/solve 两种 action。

    page_url、React keys 是旧 runner 的兼容参数，目标实现不使用它们；不注入旧环境。
    """
    _ensure_runner_environment(sdk_path)
    if action not in ('token', 'requirements'):
        raise ValueError('未知 Sentinel runner action')
    if not flow or not device_id:
        raise ValueError('flow 和 device_id 不能为空')
    if not isinstance(challenge, dict):
        raise ValueError('challenge 必须为对象')
    profile = browser_profile or {}
    ua = user_agent or str(profile.get('user_agent') or USER_AGENT)
    platform = profile.get('navigator_platform')
    if not platform:
        platform = 'iPhone' if 'iPhone' in ua else ('Win32' if 'Windows' in ua else ('MacIntel' if 'Mac' in ua else 'Linux x86_64'))
    vendor = profile.get('navigator_vendor')
    if vendor is None:
        vendor = '' if 'Firefox' in ua else ('Google Inc.' if 'Chrome' in ua else 'Apple Computer, Inc.')
    payload = {
        'action': 'requirements' if action == 'requirements' else 'solve',
        'flow': flow, 'device_id': device_id, 'user_agent': ua,
        'screen_width': profile.get('screen_width', SCREEN_WIDTH),
        'screen_height': profile.get('screen_height', SCREEN_HEIGHT),
        'language': profile.get('navigator_language', NAVIGATOR_LANGUAGE),
        'languages': list(profile.get('navigator_languages', NAVIGATOR_LANGUAGES)),
        'platform': platform, 'vendor': vendor,
        'hardware_concurrency': profile.get('hardware_concurrency', HARDWARE_CONCURRENCY),
        'device_memory': profile.get('device_memory', DEVICE_MEMORY),
        'max_touch_points': profile.get('max_touch_points', 0),
        'device_pixel_ratio': profile.get('device_pixel_ratio', 2),
        'timezone': profile.get('timezone_iana', TIMEZONE_IANA),
        'behavior_duration_ms': max(0, min(10000, int(protocol_settings.SENTINEL_SO_COLLECTION_MS))),
        'challenge': {k: v for k, v in challenge.items() if k != '_request_p'},
        'request_p': challenge.get('_request_p') or '',
        'cookie': cookie if cookie is not None else f'oai-did={device_id}',
        'sentinel_sid': sentinel_sid or '',
    }
    env = os.environ.copy()
    env['OPENAI_SENTINEL_SDK_FILE'] = str(Path(sdk_path or _SDK_PATH).resolve())
    env['TZ'] = str(payload['timezone'])
    logger.info('[SentinelRunner] Node 协议执行器 action=%s flow=%s', payload['action'], flow)
    try:
        proc = _run_node(
            [_resolve_node_executable(), str(_RUNNER_PATH)],
            input=json.dumps(payload, ensure_ascii=False), cwd=str(_PROJECT_ROOT),
            timeout=_RUNNER_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'协议 Sentinel 执行超时（>{_RUNNER_TIMEOUT}s），flow={flow}') from exc
    except FileNotFoundError as exc:
        raise RuntimeError('未找到 Node 可执行文件，请安装 Node.js 或设置 NODE_EXECUTABLE') from exc
    if proc.returncode:
        # stdout 可能包含令牌；不记录 payload/Cookie 或完整 SDK 输出。
        raise RuntimeError(f'协议 Sentinel 退出码 {proc.returncode}: {(proc.stderr or "unknown error").strip()[:1500]}')
    try:
        result = json.loads(proc.stdout or '')
    except (ValueError, TypeError) as exc:
        raise RuntimeError('协议 Sentinel 输出不是合法 JSON') from exc
    if not isinstance(result, dict):
        raise RuntimeError('协议 Sentinel 输出必须为 JSON 对象')
    if action == 'requirements':
        proof = result.get('request_p')
        if not isinstance(proof, str) or not proof.strip() or proof != proof.strip():
            raise RuntimeError('SDK requirements 未返回有效 request_p')
        return proof
    token = result.get('token')
    try:
        parsed = json.loads(token) if isinstance(token, str) else token
    except ValueError as exc:
        raise RuntimeError('SDK token 不是合法 JSON') from exc
    if not isinstance(parsed, dict):
        raise RuntimeError('SDK token 必须为 JSON 对象')
    if 'e' in parsed or 'error' in parsed:
        raise RuntimeError('SDK 返回错误 token，停止提交')
    if parsed.get('id') != device_id or parsed.get('flow') != flow:
        raise RuntimeError('SDK token 的 device_id/flow 与当前请求不匹配')
    if any(not isinstance(parsed.get(k), str) or not parsed[k].strip() for k in ('p', 'c')):
        raise RuntimeError('SDK token 缺少有效 proof/challenge')
    if parsed['c'] != challenge.get('token'):
        raise RuntimeError('SDK token 的 challenge 与当前请求不匹配')
    turnstile = challenge.get('turnstile')
    if isinstance(turnstile, dict) and (turnstile.get('required') is True or turnstile.get('dx')):
        if not isinstance(parsed.get('t'), str) or not parsed['t'].strip():
            raise RuntimeError('SDK 未返回当前 challenge 要求的 turnstile token')
    so = result.get('so_token')
    if so is not None and not isinstance(so, str):
        raise RuntimeError('SDK SO token 必须为字符串')
    if isinstance(so, str) and so and not so.strip():
        raise RuntimeError('SDK SO token 不能为空白字符串')
    so_required = isinstance(challenge.get('so'), dict) and challenge['so'].get('required') is True
    if so_required and not so:
        raise RuntimeError('SDK 未返回 SO token')
    if so:
        parsed['_so'] = so
    logger.info('[SentinelRunner] token 生成成功 flow=%s turnstile=%s so=%s fallback=%s',
                flow, bool(parsed.get('t')), bool(so), bool(result.get('fallback')))
    return json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)


def generate_sentinel_requirements(*, flow, device_id, sentinel_sid, browser_profile, sdk_path, cookie=None):
    return generate_sentinel_token({}, flow, device_id, browser_profile=browser_profile,
                                   sentinel_sid=sentinel_sid, sdk_path=sdk_path, cookie=cookie, action='requirements')
