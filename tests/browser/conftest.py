"""A local static server so browser tests never touch the internet."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixture_server(fixtures_dir: Path):
    """Serve tests/fixtures/html on a random localhost port."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(fixtures_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host!s}:{port}"
    finally:
        server.shutdown()
        server.server_close()
