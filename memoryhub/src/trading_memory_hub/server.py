from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any

from .core import EpisodeConflict, MemoryHub, MemoryHubError


def make_server(host: str, port: int, database: Path | str) -> ThreadingHTTPServer:
    hub = MemoryHub(database)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._reply(HTTPStatus.OK, hub.health())
            else:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/episodes":
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(length) or b"{}")
                self._reply(HTTPStatus.OK, hub.append(value).as_dict())
            except EpisodeConflict as error:
                self._reply(HTTPStatus.CONFLICT, {"error": "immutable_conflict", "detail": str(error)})
            except (MemoryHubError, ValueError, json.JSONDecodeError) as error:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_episode", "detail": str(error)})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _reply(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Trading MemoryHub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8820)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    make_server(args.host, args.port, args.database).serve_forever()


if __name__ == "__main__":
    main()

