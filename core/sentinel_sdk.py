"""版本化 SDK 缓存；同一认证会话固定同一份不可变 JS 文件。"""
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

from config import openai_protocol as settings

logger = logging.getLogger(__name__)
_CACHE = Path(__file__).resolve().parent.parent / 'cache' / 'sentinel-sdk'
_LOCK = threading.Lock()
_BUNDLED = Path(__file__).resolve().parent.parent / 'sentinel' / 'sdk.js'
# 仓库内置资产的来源版本，不随在线下载版本配置变化。
_BUNDLED_VERSION = '20260810913b'


@contextmanager
def _cache_lock():
    # 下载锁竞争时仍能响应任务停止，不无限阻塞在 Lock.acquire。
    from core.openai_auth import _check_stop_requested
    while not _LOCK.acquire(timeout=0.25):
        _check_stop_requested()
    try:
        _check_stop_requested()
        yield
    finally:
        _LOCK.release()


def sentinel_sdk_version(session) -> str:
    """同会话的 iframe/referer 版本也必须与已固定脚本一致。"""
    version = getattr(session, '_sentinel_sdk_version', None)
    return version if isinstance(version, str) and version else settings.SENTINEL_SV


def resolve_sentinel_sdk(session) -> Path:
    pinned = getattr(session, '_sentinel_sdk_path', None)
    if isinstance(pinned, Path):
        if not pinned.is_file():
            raise FileNotFoundError('本会话固定的 Sentinel SDK 已丢失，停止切换脚本')
        return pinned
    if not settings.SENTINEL_SDK_AUTO_UPDATE:
        if settings.SENTINEL_SV != _BUNDLED_VERSION:
            raise RuntimeError("内置 Sentinel SDK 版本与 SENTINEL_SV 不一致，请启用自动更新或恢复内置版本配置")
        if not _BUNDLED.is_file():
            raise FileNotFoundError('缺少本地 Sentinel SDK')
        session._sentinel_sdk_version = settings.SENTINEL_SV
        session._sentinel_sdk_path = _BUNDLED
        return _BUNDLED
    version = settings.SENTINEL_SV
    if not isinstance(version, str) or not version.isalnum():
        raise ValueError('SENTINEL_SV 必须为字母数字版本标识')
    from core.openai_auth import _check_stop_requested
    with _cache_lock():
        _check_stop_requested()
        directory = _CACHE / version
        manifest = directory / 'current.json'
        try:
            meta = json.loads(manifest.read_text())
            if not isinstance(meta, dict):
                raise ValueError('bad manifest')
            digest = meta['sha256']
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('bad digest')
            path = directory / (digest + '.js')
            age = time.time() - float(meta['fetched_at'])
            if (0 <= age < max(1, int(settings.SENTINEL_SDK_REFRESH_SECONDS))
                    and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
                session._sentinel_sdk_version = version
                session._sentinel_sdk_path = path
                return path
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            pass
        url = f'https://sentinel.openai.com/sentinel/{version}/sdk.js'
        headers = session._get_common_headers()
        headers.update({'accept': '*/*', 'referer': 'https://auth.openai.com/',
                        'sec-fetch-dest': 'script', 'sec-fetch-mode': 'no-cors', 'sec-fetch-site': 'same-site'})
        response = session.get(url, headers=headers)
        response.raise_for_status()
        content = response.content
        if (response.status_code != 200 or not isinstance(content, bytes)
                or not content or len(content) > 5 * 1024 * 1024
                or b'SentinelSDK' not in content or content.lstrip().startswith(b'<')):
            raise RuntimeError('Sentinel SDK 下载未返回有效脚本，不覆盖已有缓存')
        _check_stop_requested()
        digest = hashlib.sha256(content).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + '.js')
        _atomic_write(path, content)
        _atomic_write(manifest, json.dumps({'sha256': digest, 'fetched_at': time.time()}).encode())
        session._sentinel_sdk_version = version
        session._sentinel_sdk_path = path
        logger.info('[Sentinel] SDK 已更新 version=%s sha256=%s', version, digest[:12])
        return path


def _atomic_write(path, content):
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
            name = out.name
            out.write(content)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
