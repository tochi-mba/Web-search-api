"""A tiny recording HTTP server for testing SDK-based providers.

The Anthropic and OpenAI SDKs are built on ``httpx2``, which ``respx`` cannot
patch. Rather than stub the SDK methods - which would test our mocks instead of
our request shaping - these tests point the SDK's ``base_url`` at a real local
server and assert the bytes that actually go on the wire.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class RecordedRequest:
    """One request the server received."""

    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueuedResponse:
    """One response the server will hand back."""

    status: int = 200
    payload: Any = field(default_factory=dict)
    delay_seconds: float = 0.0


class MockAPIServer:
    """Serves queued responses and records what it was asked for."""

    def __init__(self) -> None:
        """Start the server on a random localhost port."""
        self.requests: list[RecordedRequest] = []
        self._responses: deque[QueuedResponse] = deque()
        self._default = QueuedResponse(200, {})
        self._lock = threading.Lock()

        server_self = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 closes the connection after each response, which keeps
            # test sockets from lingering and tripping ResourceWarning.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: Any) -> None:
                """Silence the default stderr access log."""

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {"__raw__": raw.decode("utf-8", "replace")}

                with server_self._lock:
                    server_self.requests.append(
                        RecordedRequest(
                            method=method,
                            path=self.path,
                            headers={k.lower(): v for k, v in self.headers.items()},
                            body=body,
                        )
                    )
                    response = (
                        server_self._responses.popleft()
                        if server_self._responses
                        else server_self._default
                    )

                if response.delay_seconds:
                    threading.Event().wait(response.delay_seconds)

                encoded = json.dumps(response.payload).encode()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        """Root URL clients should be pointed at."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def queue(self, payload: Any, *, status: int = 200, delay_seconds: float = 0.0) -> None:
        """Queue one response to be served in order."""
        with self._lock:
            self._responses.append(QueuedResponse(status, payload, delay_seconds))

    def always(self, payload: Any, *, status: int = 200) -> None:
        """Serve this response whenever the queue is empty."""
        self._default = QueuedResponse(status, payload)

    @property
    def last_request(self) -> RecordedRequest:
        """The most recent request received."""
        return self.requests[-1]

    def close(self) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()
