from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .core import EpisodeConflict, MemoryHub, MemoryHubError, SourceIntegrityError
from .sources import ArticleArchiveSourceAdapter, MarketHubSourceAdapter
from .derivation import DerivationWorker, OllamaExtractor
from .backup import BackupManager, BackupWorker

MAX_REQUEST_BYTES = 16 * 1024 * 1024


def make_server(host: str, port: int, database: Path | str, *, source_adapters: dict[str, Any] | None = None) -> ThreadingHTTPServer:
    adapters = source_adapters if source_adapters is not None else {
        "markethub": MarketHubSourceAdapter(), "8815": ArticleArchiveSourceAdapter(),
    }
    hub = MemoryHub(database, source_adapters=adapters)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                self._do_get()
            except SourceIntegrityError as error:
                self._reply(HTTPStatus.CONFLICT, {"error": "source_integrity", "detail": str(error)})
            except (MemoryHubError, TypeError, ValueError) as error:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(error)})

        def _do_get(self) -> None:
            request = urlsplit(self.path)
            if request.path == "/health":
                self._reply(HTTPStatus.OK, hub.health())
            elif request.path in {"/admin", "/admin/"}:
                self._asset("index.html", "text/html; charset=utf-8")
            elif request.path.startswith("/admin/assets/"):
                name = request.path.removeprefix("/admin/assets/")
                content_type = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
                self._asset(name, content_type)
            elif request.path == "/v1/admin/memory-spaces":
                self._reply(HTTPStatus.OK, {"result": hub.admin_memory_spaces()})
            elif request.path == "/v1/admin/episodes":
                query = parse_qs(request.query)
                cursor = _first(query, "cursor")
                result = hub.admin_episodes(
                    _first(query, "memory_space_id") or "",
                    cursor=int(cursor) if cursor else None,
                    limit=int(_first(query, "limit") or "50"),
                    **_admin_episode_filters(query),
                )
                self._reply(HTTPStatus.OK, {"result": result})
            elif request.path.startswith("/v1/admin/episodes/") and request.path.endswith("/source"):
                episode_id = unquote(request.path.removeprefix("/v1/admin/episodes/").removesuffix("/source"))
                self._reply(HTTPStatus.OK, {"result": hub.admin_source(episode_id)})
            elif request.path.startswith("/v1/admin/episodes/") and request.path != "/v1/admin/episodes/export":
                episode_id = unquote(request.path.removeprefix("/v1/admin/episodes/"))
                self._reply(HTTPStatus.OK, {"result": hub.admin_episode(episode_id)})
            elif request.path == "/v1/admin/timeline":
                query = parse_qs(request.query)
                self._reply(HTTPStatus.OK, {"result": hub.timeline(_first(query, "memory_space_id") or "")})
            elif request.path == "/v1/admin/episodes/export":
                query = parse_qs(request.query)
                result = hub.admin_episodes(_first(query, "memory_space_id") or "", limit=10000,
                                            **_admin_episode_filters(query))
                self._download("memoryhub-episodes.json", {"result": result})
            else:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_REQUEST_BYTES:
                    self._reply(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "request_too_large", "limit_bytes": MAX_REQUEST_BYTES},
                    )
                    return
                value = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/v1/episodes":
                    result: Any = hub.append(value).as_dict()
                elif self.path == "/v1/episodes/batch":
                    result = hub.append_batch(value["episodes"])
                elif self.path == "/v1/snapshots":
                    result = hub.begin_snapshot(
                        value["memory_space_id"], as_of=value["as_of"],
                        stage=value["stage"], cycle_id=value.get("cycle_id"),
                    ).as_dict()
                else:
                    parts = self.path.strip("/").split("/")
                    if self.path == "/v1/retrieval/query-sets":
                        result = hub.create_frozen_query_set(value["memory_space_id"], value["queries"])
                        self._reply(HTTPStatus.OK, {"result": result})
                        return
                    if self.path == "/v1/retrieval/evaluations":
                        result = hub.evaluate_candidate(value["query_set_id"], value["candidate"])
                        self._reply(HTTPStatus.OK, {"result": result})
                        return
                    if self.path == "/v1/retrieval/promotions":
                        result = hub.promote_candidate(value["report_id"])
                        self._reply(HTTPStatus.OK, {"result": result})
                        return
                    if len(parts) == 4 and parts[:2] == ["v1", "retrieval"] and parts[2] == "audits":
                        self._reply(HTTPStatus.OK, {"result": hub.retrieval_audit(parts[3])})
                        return
                    if len(parts) == 4 and parts[:2] == ["v1", "retrieval"] and parts[2] == "bundles":
                        self._reply(HTTPStatus.OK, {"result": hub.replay_bundle(parts[3])})
                        return
                    if len(parts) >= 3 and parts[:2] == ["v1", "memory-spaces"]:
                        parts[2] = unquote(parts[2])
                    if len(parts) == 4 and parts[:2] == ["v1", "memory-spaces"] and parts[3] == "timeline":
                        result = hub.timeline(parts[2], after_sequence=value.get("after_sequence", 0))
                        self._reply(HTTPStatus.OK, {"result": result})
                        return
                    if len(parts) == 4 and parts[:2] == ["v1", "memory-spaces"] and parts[3] == "export":
                        self._reply(HTTPStatus.OK, {"result": hub.export_space(parts[2])})
                        return
                    if len(parts) == 5 and parts[:2] == ["v1", "memory-spaces"] and parts[3:] == ["clear", "prepare"]:
                        self._reply(HTTPStatus.OK, {"result": hub.prepare_clear(parts[2], value["export_sha256"])})
                        return
                    if len(parts) == 4 and parts[:2] == ["v1", "memory-spaces"] and parts[3] == "clear":
                        self._reply(HTTPStatus.OK, {"result": hub.clear_space(parts[2], value["confirmation_token"])})
                        return
                    if len(parts) != 4 or parts[:2] != ["v1", "snapshots"]:
                        self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                        return
                    snapshot_id, operation = parts[2], parts[3]
                    if operation == "search":
                        result = hub.search(snapshot_id, value.get("query", ""), limit=value.get("limit", 20))
                    elif operation == "retrieve":
                        result = hub.retrieve_bundle(snapshot_id, value.get("query", ""), limit=value.get("limit", 20))
                    elif operation == "expand":
                        result = hub.expand(snapshot_id, value["episode_id"])
                    elif operation == "related":
                        result = hub.related(snapshot_id, value["episode_id"], limit=value.get("limit", 20))
                    else:
                        self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                        return
                self._reply(HTTPStatus.OK, {"result": result})
            except EpisodeConflict as error:
                self._reply(HTTPStatus.CONFLICT, {"error": "immutable_conflict", "detail": str(error)})
            except SourceIntegrityError as error:
                self._reply(HTTPStatus.CONFLICT, {"error": "source_integrity", "detail": str(error)})
            except (KeyError, MemoryHubError, TypeError, ValueError, json.JSONDecodeError) as error:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_episode", "detail": str(error)})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _reply(self, status: HTTPStatus, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _asset(self, name: str, content_type: str) -> None:
            if name not in {"index.html", "admin.css", "admin.js"}:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            body = files("trading_memory_hub.admin").joinpath(name).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _download(self, filename: str, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name) or []
    return values[0] if values else None


def _admin_episode_filters(query: dict[str, list[str]]) -> dict[str, str]:
    return {"query": _first(query, "q") or "", **{
        name: _first(query, name) or "" for name in (
            "episode_type", "source_system", "authority", "derivation_state",
            "occurred_from", "occurred_to", "known_from", "known_to",
            "submitted_from", "submitted_to",
        )}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Trading MemoryHub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8820)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ollama-model", default="")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--backup-interval-seconds", type=float, default=86400.0)
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.database)
    worker = None
    backup_worker = None
    if args.ollama_model:
        worker = DerivationWorker(
            MemoryHub(args.database),
            OllamaExtractor(base_url=args.ollama_url, model=args.ollama_model),
        )
    if args.backup_dir:
        backup_worker = BackupWorker(
            BackupManager(MemoryHub(args.database)), args.backup_dir,
            interval_seconds=args.backup_interval_seconds,
        )
    try:
        if worker:
            worker.start()
        if backup_worker:
            backup_worker.start()
        server.serve_forever()
    finally:
        if worker:
            worker.stop()
        if backup_worker:
            backup_worker.stop()


if __name__ == "__main__":
    main()
