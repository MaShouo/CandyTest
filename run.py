"""Start CandyTest in local or reverse-proxy server mode."""
from __future__ import annotations

import os
import sys
import threading
import webbrowser

from waitress import serve

from candytest.app import configured_deployment, configured_host, configured_port, create_app


def browser_enabled(deployment: str) -> bool:
    return deployment == "local" and os.environ.get("CANDYTEST_OPEN_BROWSER", "1") != "0"


def configure_console() -> None:
    """Keep status output from crashing under a non-UTF Windows code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def main() -> None:
    configure_console()
    deployment = configured_deployment()
    host, port = configured_host(deployment), configured_port()
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{port}/"

    if deployment == "local":
        print(f"CandyTest 正在本机回环地址监听：{url}")
        if browser_enabled(deployment):
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    else:
        print(f"CandyTest 服务器模式正在监听：{url}")
        print("请仅通过配置 HTTPS 与认证的反向代理访问；不要直接将此监听端口暴露到公网。")

    serve(create_app(), host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
