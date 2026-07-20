"""
analysis/screening/dashboard_server.py
-----------------------------------------
Local-only web server for the screening dashboard. "LOCAL" means only your
own machine can reach it -- nothing is exposed to the internet.

start_dashboard_server() runs the server in a background daemon thread so
it can be called from a long-running MCP server process without blocking
it. Calling it twice is safe -- if the port is already bound (e.g. from a
previous call in this same process), it just returns the existing URL
instead of crashing.
"""

import http.server
import socketserver
import threading
import urllib.parse
import webbrowser
from pathlib import Path

# analysis/screening/dashboard/vaulter_dashboard.html
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"

# Must match vaulter_dashboard.html's OUTPUT_DIR constant exactly.
_SCREENING_OUTPUT_URL_PREFIX = "/data/output/screening/"

_server_lock = threading.Lock()
_running_servers: dict[int, str] = {}  # port -> url


def _make_handler(root_dir: Path, screening_output_dir: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root_dir), **kwargs)

        def log_message(self, format, *args):
            pass  # quiet -- suppress routine request logging

        def translate_path(self, path):
            # The dashboard's own HTML/JS/CSS still serve relative to
            # root_dir as before. But SCREENING_OUTPUT_DIR (manifest.json +
            # workbooks) now lives in the shared OneDrive folder -- NOT
            # inside root_dir -- so requests for it are redirected to the
            # real absolute path instead of resolving relative to root_dir
            # like every other request.
            #
            # SECURITY: rel must be verified by RESOLVING it and checking
            # it's genuinely inside safe_root -- pattern-matching the raw
            # string (e.g. rejecting a leading "..") is not enough. A path
            # like "/data/output/screening//etc/passwd" normalizes to the
            # ABSOLUTE path "/etc/passwd", and Path(dir) / "/etc/passwd"
            # silently discards `dir` entirely (that's how pathlib's `/`
            # operator handles an absolute right-hand side) -- so a naive
            # "doesn't start with .." check would have let this through.
            unquoted = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
            if unquoted.startswith(_SCREENING_OUTPUT_URL_PREFIX):
                rel = unquoted[len(_SCREENING_OUTPUT_URL_PREFIX):].lstrip("/")
                safe_root = screening_output_dir.resolve()
                candidate = (safe_root / rel).resolve()
                if candidate != safe_root and safe_root not in candidate.parents:
                    return str(safe_root / "__invalid_path__")  # clean 404, escape attempt blocked
                return str(candidate)
            return super().translate_path(path)

    return Handler


def start_dashboard_server(root_dir: Path, screening_output_dir: Path, port: int = 8000) -> str:
    """
    Starts a socketserver.TCPServer in a background daemon thread, bound to
    127.0.0.1:port. The dashboard's own static assets serve relative to
    root_dir (the project root); manifest.json and the result workbooks
    serve from screening_output_dir instead, wherever that actually lives
    (the shared OneDrive folder, not necessarily inside root_dir) -- see
    translate_path above. Returns the dashboard URL string.
    """
    url = f"http://127.0.0.1:{port}/analysis/screening/dashboard/vaulter_dashboard.html"

    with _server_lock:
        if port in _running_servers:
            return _running_servers[port]

        handler = _make_handler(root_dir, screening_output_dir)
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
        except OSError:
            # Address already in use -- likely already running from a prior
            # call (possibly in a different process). Return the expected
            # URL anyway since something is already bound to this port.
            _running_servers[port] = url
            return url

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        _running_servers[port] = url
        return url


if __name__ == "__main__":
    # Manual-testing entry point -- old blocking standalone behavior.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from config import SCREENING_OUTPUT_DIR

    PORT = 8000
    ROOT_DIR = Path(__file__).resolve().parent.parent.parent  # project root

    with socketserver.TCPServer(("127.0.0.1", PORT), _make_handler(ROOT_DIR, SCREENING_OUTPUT_DIR)) as httpd:
        url = f"http://127.0.0.1:{PORT}/analysis/screening/dashboard/vaulter_dashboard.html"
        print(f"Serving locally at {url}")
        print("This is only reachable from your own machine -- nothing is exposed externally.")
        print("Press Ctrl+C to stop.\n")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
