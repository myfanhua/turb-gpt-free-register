# -*- coding: utf-8 -*-
"""
Sentinel Runner 适配层
通过 subprocess 调用项目根目录的 sentinel-runner.js，
让 Node.js 在 vm 沙箱中真实运行 sdk.js，生成可通过校验的 sentinel-token。

工作原理：
1. Python 端先调用 sentinel.openai.com/backend-api/sentinel/req 拿到 challenge JSON
2. 把 challenge 写入临时文件
3. 调用 node sentinel-runner.js --challenge-file <临时文件> ...
4. 捕获 stdout 即为 openai-sentinel-token 的 value
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from config import (
    USER_AGENT,
    CHROME_MAJOR,
    CHROME_FULL_VERSION,
    SEC_CH_UA_PLATFORM,  # noqa: F401（保留给后续扩展）
    TIMEZONE_IANA,
    NAVIGATOR_LANGUAGE,
    NAVIGATOR_LANGUAGES,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    HARDWARE_CONCURRENCY,
    JS_HEAP_SIZE_LIMIT,
    DEVICE_MEMORY,
    SENTINEL_SV,
    OPENAI_BUILD_ID,
)

logger = logging.getLogger(__name__)

# 项目根目录（core 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Node 资源放在项目根的 sentinel/ 子目录下
_SENTINEL_DIR = _PROJECT_ROOT / "sentinel"
_RUNNER_PATH = _SENTINEL_DIR / "sentinel-runner.js"
_SDK_PATH = _SENTINEL_DIR / "sdk.js"

# 各 flow 对应的 page-url（与浏览器实际页面一致，影响 sdk.js 指纹生成）
_FLOW_PAGE_URL = {
    "username_password_create": "https://auth.openai.com/create-account/password",
    "authorize_continue": "https://auth.openai.com/email-verification",
    "oauth_create_account": "https://auth.openai.com/about-you",
}

# Node 子进程超时（秒）。sdk.js 内部可能要做 PoW，留充裕一点
_RUNNER_TIMEOUT = 60


def _resolve_node_executable() -> str:
    """
    解析 Node 可执行文件名。Windows 下默认 node.exe，类 Unix 为 node。
    允许通过环境变量 NODE_EXECUTABLE 覆盖。
    """
    override = os.environ.get("NODE_EXECUTABLE")
    if override:
        return override
    return "node.exe" if sys.platform.startswith("win") else "node"


def _ensure_runner_environment() -> None:
    """启动前的强制检查：runner.js / sdk.js 必须存在。"""
    if not _RUNNER_PATH.exists():
        raise FileNotFoundError(f"找不到 sentinel-runner.js: {_RUNNER_PATH}")
    if not _SDK_PATH.exists():
        raise FileNotFoundError(f"找不到 sdk.js: {_SDK_PATH}")


def generate_sentinel_token(
    challenge: dict,
    flow: str,
    device_id: str,
    user_agent: str | None = None,
    page_url: str | None = None,
    browser_profile: dict | None = None,
    sentinel_sid: str | None = None,
    react_listening_key: str | None = None,
) -> str:
    """
    把 sentinel.openai.com 返回的 challenge 喂给 sdk.js，生成最终 sentinel-token 字符串。

    Args:
        challenge: sentinel/req 返回的完整 JSON（含 token / proofofwork / turnstile / so 字段）
        flow: 流程标识，例如 username_password_create / authorize_continue / oauth_create_account
        device_id: oai-did，必须与 Python 端 BrowserSession 持有的同一个值
        user_agent: 必须与 Python 端请求 UA 完全一致；默认读取 config.USER_AGENT
        page_url: 当前所在页面 URL（影响 referer / location 指纹）；默认按 flow 推断

    Returns:
        openai-sentinel-token 头的完整字符串值（runner 的 stdout 原样返回，已是 JSON 字符串）

    Raises:
        FileNotFoundError: runner.js 或 sdk.js 缺失
        RuntimeError: Node 子进程异常或返回非零退出码
    """
    _ensure_runner_environment()

    if not flow:
        raise ValueError("flow 不能为空")
    if not device_id:
        raise ValueError("device_id 不能为空")

    profile = browser_profile or {}
    browser_family = str(profile.get("browser_family") or "safari")
    request_idle_callback = int((profile.get("window_feature_flags") or {}).get("requestIdleCallback", 0))
    ua = user_agent or str(profile.get("user_agent") or USER_AGENT)
    screen_width = int(profile.get("screen_width", SCREEN_WIDTH))
    screen_height = int(profile.get("screen_height", SCREEN_HEIGHT))
    hardware_concurrency = int(profile.get("hardware_concurrency", HARDWARE_CONCURRENCY))
    js_heap_size_limit = int(profile.get("js_heap_size_limit", JS_HEAP_SIZE_LIMIT))
    device_memory = int(profile.get("device_memory", DEVICE_MEMORY))
    device_pixel_ratio = float(profile.get("device_pixel_ratio", 2))
    navigator_language = str(profile.get("navigator_language", NAVIGATOR_LANGUAGE))
    navigator_languages = list(profile.get("navigator_languages", NAVIGATOR_LANGUAGES))
    chrome_major = str(profile.get("chrome_major", CHROME_MAJOR))
    chrome_full_version = str(profile.get("chrome_full_version", CHROME_FULL_VERSION))
    build_id = str(profile.get("build_id", OPENAI_BUILD_ID))
    timezone_iana = str(profile.get("timezone_iana", TIMEZONE_IANA))

    page = page_url or _FLOW_PAGE_URL.get(
        flow, "https://auth.openai.com/create-account/password"
    )

    # 把 challenge 写入临时文件，避免命令行长度 / 转义问题
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=f"sentinel-challenge-{flow}-",
        delete=False,
        encoding="utf-8",
    )
    try:
        json.dump(challenge, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()

        cmd = [
            _resolve_node_executable(),
            str(_RUNNER_PATH),
            "--challenge-file", tmp.name,
            "--flow", flow,
            "--device-id", device_id,
            "--sentinel-sid", sentinel_sid or "",
            "--react-listening-key", react_listening_key or "",
            "--page-url", page,
            "--user-agent", ua,
            "--browser-family", browser_family,
            "--request-idle-callback", "1" if request_idle_callback else "0",
            "--sdk", str(_SDK_PATH),
            "--script-src", f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js",
            "--build-id", build_id,
            # 与 config.browser / core.sentinel.py 中的指纹默认值保持一致
            "--width", str(screen_width),
            "--height", str(screen_height),
            "--cores", str(hardware_concurrency),
            "--language", navigator_language,
            "--languages", ",".join(navigator_languages),
            "--js-heap-size-limit", str(js_heap_size_limit),
            "--device-memory", str(device_memory),
            "--device-pixel-ratio", str(device_pixel_ratio),
            "--chrome-major", chrome_major,
            "--chrome-full-version", chrome_full_version,
            "--no-cookie",
        ]

        logger.info(f"[SentinelRunner] 调用 Node 生成 token, flow={flow}")
        logger.debug(f"[SentinelRunner] 命令: {' '.join(cmd)}")

        # 关键：禁用 sentinel.config.json 自动发现（避免外部配置干扰）
        env = os.environ.copy()
        env.pop("SENTINEL_CONFIG", None)
        env["SENTINEL_CONFIG"] = "__none__"  # 故意指向不存在的文件，跳过 fallback 列表
        env["TZ"] = timezone_iana  # 让 Node VM 里的 Date.toString() 与 Python p 指纹时区一致

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(_PROJECT_ROOT),
                timeout=_RUNNER_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"sentinel-runner.js 执行超时（>{_RUNNER_TIMEOUT}s），flow={flow}"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "未找到 Node 可执行文件，请确认已安装 Node.js 并加入 PATH，"
                "或通过 NODE_EXECUTABLE 环境变量指定绝对路径。"
            ) from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            raise RuntimeError(
                f"sentinel-runner.js 退出码 {proc.returncode}\n"
                f"stderr: {stderr}\n"
                f"stdout: {stdout}"
            )

        token_text = (proc.stdout or "").strip()
        if not token_text:
            raise RuntimeError(
                f"sentinel-runner.js 输出为空, stderr: {(proc.stderr or '').strip()}"
            )

        # 简单合法性校验：必须是合法 JSON 且包含关键字段
        try:
            parsed = json.loads(token_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"runner 输出不是合法 JSON: {token_text[:200]}"
            ) from exc

        for required_key in ("p", "c", "id", "flow"):
            if required_key not in parsed:
                raise RuntimeError(
                    f"runner 输出缺少字段 {required_key}: {token_text[:200]}"
                )

        # 详细诊断：打印输出 JSON 的所有顶层字段名 + 值长度
        field_summary = {
            k: (len(v) if isinstance(v, str) else type(v).__name__)
            for k, v in parsed.items()
        }
        logger.info(
            f"[SentinelRunner] token 生成成功, flow={flow}, "
            f"包含 turnstile={'t' in parsed and bool(parsed.get('t'))}, "
            f"包含 so={bool(parsed.get('so'))}, "
            f"字段: {field_summary}"
        )
        return token_text

    finally:
        # 清理临时文件
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
