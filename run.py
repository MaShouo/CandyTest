"""Start CandyTest in local or server deployment mode."""
from __future__ import annotations

import sys
import threading
import webbrowser

from waitress import serve

from candytest.app import configured_deployment, configured_host, configured_port, create_app


def configure_console() -> None:
    """Keep status output safe on non-UTF Windows consoles and CI logs."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def main() -> None:
    configure_console()
    deployment = configured_deployment()
    host, port = configured_host(), configured_port()
    app = create_app()

    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{port}/"
    if deployment == "local":
        print(f"CandyTest is listening locally at {url}")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    else:
        print(f"CandyTest server mode is listening at {url}; use HTTPS through your reverse proxy.")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
