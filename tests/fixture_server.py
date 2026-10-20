"""A local HTTP server for the fixture pages in `tests/fixtures/`.

The scraping tests and the benchmark script both need to exercise the *real*
fetch path — redirects, status codes, content types, byte caps — not a mocked
one, because that path is where the SSRF re-validation and the politeness
controls live. Mocking `requests` would test everything except the part most
worth testing.

So this serves the fixtures on loopback and adds the awkward cases a real crawl
has to survive: a redirect chain, a redirect to a blocked address, 403, 429, a
slow page, a non-HTML response, and a page far larger than the byte cap.

Because the target is 127.0.0.1, a test using this must set
`ALLOW_PRIVATE_FETCH=1` and `REVIEWBAY_ENV=dev`; `urlpolicy` deliberately
requires both, so a production deployment cannot be talked into fetching
loopback by setting one variable.
"""

from __future__ import annotations

import http.server
import os
import socket
import threading
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

#: Routes that are not plain files. path -> (status, headers, body)
_SPECIAL: dict[str, tuple[int, dict, bytes]] = {}


def _build_special() -> None:
    _SPECIAL.clear()
    _SPECIAL["/redirect-once"] = (
        302, {"Location": "/structural_reviews.html"}, b""
    )
    _SPECIAL["/redirect-chain"] = (302, {"Location": "/redirect-once"}, b"")
    # The interesting SSRF case: a policy-clean host redirecting somewhere it
    # must not be followed to.
    _SPECIAL["/redirect-to-metadata"] = (
        302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b""
    )
    _SPECIAL["/redirect-loop"] = (302, {"Location": "/redirect-loop"}, b"")
    _SPECIAL["/forbidden"] = (403, {}, b"<html><body>Forbidden</body></html>")
    _SPECIAL["/rate-limited"] = (429, {"Retry-After": "60"}, b"<html><body>Slow down</body></html>")
    _SPECIAL["/server-error"] = (500, {}, b"<html><body>Oops</body></html>")
    _SPECIAL["/not-html"] = (
        200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 fake pdf bytes"
    )
    _SPECIAL["/robots.txt"] = (
        200,
        {"Content-Type": "text/plain"},
        b"User-agent: *\nDisallow: /private/\nCrawl-delay: 0\n",
    )
    _SPECIAL["/private/secret.html"] = (
        200, {}, b"<html><body><div class='review'>Should never be fetched.</div></body></html>"
    )
    # A page whose body exceeds the default 3MB cap, to prove truncation works
    # and does not become a MemoryError.
    big = b"<html><body><div class='review'><p>" + (b"padding " * 600_000) + b"</p></div></body></html>"
    _SPECIAL["/huge.html"] = (200, {}, big)
    # A client-rendered shell: the browser-escalation heuristic should fire here.
    _SPECIAL["/js-shell.html"] = (
        200,
        {},
        b"<html><head><title>Loading</title></head><body><div id=\"root\"></div>"
        b"<script>window.__INITIAL_STATE__={}</script></body></html>",
    )


_build_special()


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ReviewBayFixtures/1.0"

    def log_message(self, *args) -> None:  # keep test output readable
        pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's API
        path = self.path.split("?")[0]

        if path == "/slow":
            time.sleep(float(self.headers.get("X-Delay", "5")))
            self._send(200, {}, b"<html><body>late</body></html>")
            return

        if path in _SPECIAL:
            status, headers, body = _SPECIAL[path]
            self._send(status, headers, body)
            return

        name = path.lstrip("/")
        target = (FIXTURES / name).resolve()
        # Keep the fixture directory a hard boundary even in a test helper.
        if not name or FIXTURES.resolve() not in target.parents or not target.is_file():
            self._send(404, {}, b"<html><body>not found</body></html>")
            return
        self._send(200, {}, target.read_bytes())

    def _send(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        if "Content-Type" not in headers:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client hit its byte cap and hung up; that is the test


class FixtureServer:
    """`with FixtureServer() as base_url:` — serves fixtures on a free port."""

    def __init__(self) -> None:
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(self) -> str:
        self.port = self._free_port()
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


def dev_fetch_env() -> dict[str, str]:
    """The environment a test needs to be allowed to fetch loopback.

    Both variables are required by `urlpolicy._dev_private_allowed()`, which is
    why a single misconfigured variable in production cannot open this up.
    """
    return {
        "ALLOW_PRIVATE_FETCH": "1",
        "REVIEWBAY_ENV": "dev",
        # The fixture server serves a robots.txt with Crawl-delay: 0, so leave
        # robots ON by default: the tests should exercise the real check.
        "SCRAPING_DEFAULT_DELAY": "0",
        "SCRAPING_ROBOTS_TTL": "1",
    }


def apply_dev_env() -> None:
    os.environ.update(dev_fetch_env())
