# -*- coding: utf-8 -*-
"""
WebUI 启动入口。

用法：
    python web.py                 # 默认 http://127.0.0.1:5000，仅本地访问，不自动打开浏览器
    python web.py --open-browser  # 启动后自动打开浏览器
    python web.py --port 8000     # 换端口
    python web.py --host 0.0.0.0  # 允许局域网访问（敏感工具，自行评估）

与 CLI（python main.py）完全平行，互不影响。
"""
import argparse
import logging
import webbrowser
from threading import Timer

from webui.app import create_app


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT 注册 WebUI 控制台")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址，默认仅本地 127.0.0.1")
    parser.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    app = create_app()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    logger.info(f"WebUI 已启动：{url}")
    if args.host in ("0.0.0.0", "::"):
        logger.warning("已绑定到所有网卡，局域网内其他设备可访问。这是敏感工具，请确认网络环境可信。")

    # 默认不自动打开浏览器；需要时显式传 --open-browser
    if args.open_browser:
        Timer(1.0, lambda: webbrowser.open(url)).start()

    # debug=False：避免 reloader 双进程导致线程池/定时器重复
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
