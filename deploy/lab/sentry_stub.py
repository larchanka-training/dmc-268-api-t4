"""A stand-in for Sentry's ingest, for the observability lab (deploy/lab/README.md).

The app's Sentry SDK posts its envelopes here (SENTRY_DSN=http://lab@sentry:9000/1), so
lab.py can check what Sentry would receive: error events, transactions, and that the
scrubbers kept secrets and bodies out. Not a Sentry: no grouping, no alerts, no UI.

    GET    /items   every item kept, oldest first: [{"type": ..., "payload": {...}}]
    DELETE /items   forget them

Standard library only; runs in the app image's base, python:3.13-slim.
"""

import gzip
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 9000
#: The newest items are kept; a lab runs for an hour or so.
MAX_ITEMS = 5000

_items: list[dict[str, Any]] = []
_lock = threading.Lock()


def parse_envelope(body: bytes) -> list[dict[str, Any]]:
    """The items of a Sentry envelope: a header line, then per item a header line and its
    payload, which is ``length`` bytes when the header says so, else one line."""
    _, _, rest = body.partition(b"\n")
    items = []
    while rest.strip():
        header_line, _, rest = rest.partition(b"\n")
        if not header_line.strip():
            continue
        header = json.loads(header_line)
        length = header.get("length")
        if isinstance(length, int):
            payload, rest = rest[:length], rest[length:].removeprefix(b"\n")
        else:
            payload, _, rest = rest.partition(b"\n")
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        items.append({"type": header.get("type"), "payload": data})
    return items


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        encoding = self.headers.get("Content-Encoding", "")
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body)
        if self.path.rstrip("/").endswith("/envelope"):
            items = parse_envelope(body)
        else:  # the legacy store endpoint: one event
            items = [{"type": "event", "payload": json.loads(body)}]
        with _lock:
            _items.extend(items)
            del _items[:-MAX_ITEMS]
        self._answer(200, {})

    def do_GET(self) -> None:
        if self.path != "/items":
            self._answer(404, {"error": "not found"})
            return
        with _lock:
            self._answer(200, list(_items))

    def do_DELETE(self) -> None:
        with _lock:
            _items.clear()
        self._answer(200, {})

    def _answer(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet: the SDK posts often


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
