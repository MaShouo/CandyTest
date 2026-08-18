"""Start CandyTest and open the local-only web interface."""
from __future__ import annotations

import threading
import webbrowser

from waitress import serve

from candytest.app import (
    configured_host,
    configured_port,
    create_app,
    perform_shutdown_sync,
    perform_startup_sync,
)


def _report_sync(prefix: str, state: dict) -> None:
    status = state.get("status")
    if status == "completed":
        revision = (state.get("result") or {}).get("revision", "unknown")
        print(f"WebDAV {prefix} completed: {revision}")
    elif status in {"failed", "skipped"}:
        code = (state.get("error") or {}).get("code", "WEBDAV_ERROR")
        print(f"WebDAV {prefix} {status}; continuing locally ({code}).")


def main() -> None:
    host, port = configured_host(), configured_port()
    app = create_app()
    _report_sync("startup pull", perform_startup_sync(app))

    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{port}/"
    print(f"CandyTest is listening locally at {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        serve(app, host=host, port=port, threads=8)
    finally:
        _report_sync("shutdown push", perform_shutdown_sync(app))


if __name__ == "__main__":
    main()
