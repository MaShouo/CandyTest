"""Start CandyTest and open the local-only web interface."""
from __future__ import annotations

import threading
import webbrowser

from waitress import serve

from candytest.app import configured_host, configured_port, create_app


if __name__ == "__main__":
    host, port = configured_host(), configured_port()
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{port}/"
    print(f"CandyTest is listening locally at {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    serve(create_app(), host=host, port=port, threads=8)
