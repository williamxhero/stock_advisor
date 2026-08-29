"""Authenticated loopback Gateway for the local desktop client."""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from .store import CompanionStore


PROTOCOL = "companion-gateway/v1"


class RuntimeGateway:
    def __init__(self, home: Path, store: CompanionStore,
                 command: Callable[[dict[str, Any]], dict[str, Any]],
                 snapshot: Callable[[str, web.Request], dict[str, Any]],
                 tick: Callable[[], None]) -> None:
        self._home, self._store, self._command, self._snapshot, self._tick = home, store, command, snapshot, tick
        self._command_lock = asyncio.Lock()
        # This is a process-local loopback credential for the desktop/runtime
        # boundary, unrelated to the remote LLM Broker.
        self._token = secrets.token_urlsafe(48)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=1_048_576, middlewares=[self._auth])
        app.router.add_get("/v1/health", self.health)
        app.router.add_get("/v1/snapshots/{kind}", self.snapshots)
        app.router.add_post("/v1/commands", self.commands)
        app.router.add_get("/v1/events", self.events)
        app.on_startup.append(self._start_ticker)
        app.on_cleanup.append(self._stop_ticker)
        return app

    @web.middleware
    async def _auth(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        peer = request.remote or ""
        if peer not in {"127.0.0.1", "::1"}:
            raise web.HTTPForbidden(text="loopback only")
        if request.headers.get("Authorization") != f"Bearer {self._token}":
            raise web.HTTPUnauthorized(text="gateway authentication required")
        return await handler(request)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"contract": PROTOCOL, "state": "running", "database": "ready"})

    async def snapshots(self, request: web.Request) -> web.Response:
        return web.json_response(self._snapshot(request.match_info["kind"], request))

    async def commands(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="command must be an object")
        try:
            async with self._command_lock:
                result = await asyncio.to_thread(self._command, body)
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"contract": "companion-command-receipt/v1", "command_id": body.get("command_id"), "state": "accepted", "result": result}, status=202)

    async def events(self, request: web.Request) -> web.StreamResponse:
        try:
            after = max(0, int(request.query.get("after", "0")))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="after must be an integer") from exc
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"})
        await response.prepare(request)
        while not request.transport.is_closing():
            rows = await asyncio.to_thread(self._store.client_events, after)
            for row in rows:
                after = int(row["sequence"])
                payload = {"contract": row["contract"], "sequence": after, "event_id": row["event_id"],
                           "cycle_id": row["cycle_id"], "type": row["event_type"], "created_at": row["created_at"],
                           "payload": json.loads(row["payload_json"])}
                await response.write(f"id: {after}\nevent: change\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            await asyncio.sleep(0.5)
        return response

    async def _start_ticker(self, app: web.Application) -> None:
        async def loop() -> None:
            while True:
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    pass
                await asyncio.sleep(5)
        app["ticker"] = asyncio.create_task(loop())

    async def _stop_ticker(self, app: web.Application) -> None:
        task = app.get("ticker")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def write_descriptor(self, runner: web.AppRunner) -> None:
        site = next(iter(runner.sites))
        sockets = getattr(site, "_server").sockets
        port = sockets[0].getsockname()[1]
        target = self._home / "runtime" / "gateway.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"contract": PROTOCOL, "host": "127.0.0.1", "port": port, "token": self._token}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)


async def serve(gateway: RuntimeGateway) -> None:
    runner = web.AppRunner(gateway.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    await gateway.write_descriptor(runner)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
