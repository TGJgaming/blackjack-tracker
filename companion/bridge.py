"""Localhost-only async WebSocket bridge connecting companion state machine & calibrator to index.html."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from typing import Any, Callable

from websockets.asyncio.server import Server, ServerConnection, serve

ALLOWED_ORIGINS = [
    None,
    re.compile(r"^null$"),
    re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$"),
    re.compile(r"^chrome-extension://[a-p]{32}$"),
]

MessageCallback = Callable[[dict[str, Any]], Any]


class CompanionBridge:
    """Broadcasts companion events to connected local tracker tabs and dispatches inbound RPCs."""

    def __init__(
        self,
        host: str,
        port: int,
        on_message: MessageCallback,
        on_connect: Callable[[], list[dict[str, Any]]] | None = None,
        on_ext_disconnect: Callable[[], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_ext_disconnect = on_ext_disconnect
        self.clients: set[ServerConnection] = set()
        self.ext_clients: set[ServerConnection] = set()
        self._ext_ack_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._server: Server | None = None

    @property
    def has_extension_client(self) -> bool:
        return bool(self.ext_clients)

    async def _dispatch(self, conn: ServerConnection, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "browser_ext_ping":
            self.ext_clients.add(conn)
            await conn.send(json.dumps({"type": "browser_ext_pong"}))
            return
        if mtype == "browser_ext_hello":
            self.ext_clients.add(conn)
            res = self.on_message(msg)
            if inspect.isawaitable(res):
                res = await res  # type: ignore[misc]
            await conn.send(json.dumps({"type": "browser_ext_ack", "ok": True}))
            return
        if mtype == "ext_reload_ack":
            req_id = str(msg.get("requestId") or "")
            fut = self._ext_ack_waiters.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        res = self.on_message(msg)
        if inspect.isawaitable(res):
            res = await res  # type: ignore[misc]
        if isinstance(res, dict):
            await conn.send(json.dumps(res))
        elif isinstance(res, list):
            for item in res:
                await conn.send(json.dumps(item))

    async def _handler(self, conn: ServerConnection) -> None:
        self.clients.add(conn)
        try:
            if self.on_connect is not None:
                for ev in self.on_connect():
                    await conn.send(json.dumps(ev))
            async for raw in conn:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(conn, msg)
        except Exception:
            pass
        finally:
            was_ext = conn in self.ext_clients
            self.clients.discard(conn)
            self.ext_clients.discard(conn)
            if was_ext and not self.ext_clients and self.on_ext_disconnect is not None:
                try:
                    out = self.on_ext_disconnect()
                    if inspect.isawaitable(out):
                        await out
                except Exception:
                    pass

    async def start(self) -> None:
        self._server = await serve(
            self._handler,
            self.host,
            self.port,
            origins=ALLOWED_ORIGINS,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def broadcast(self, event: dict[str, Any]) -> None:
        targets = [c for c in list(self.clients) if c not in self.ext_clients]
        if not targets:
            return
        payload = json.dumps(event)
        stale: list[ServerConnection] = []
        for conn in targets:
            try:
                await conn.send(payload)
            except Exception:
                stale.append(conn)
        for conn in stale:
            self.clients.discard(conn)
            self.ext_clients.discard(conn)

    async def request_extension_reload(
        self,
        title_substrings: list[str],
        *,
        prefer_left: bool = True,
        bypass_cache: bool = False,
        timeout_sec: float = 1.8,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Ask a connected Brave MV3 extension client to reload the matched table tab natively."""
        if not self.ext_clients:
            return False, "ext_not_connected", None
        conn = list(self.ext_clients)[-1]
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._ext_ack_waiters[req_id] = fut
        payload = json.dumps(
            {
                "type": "ext_reload_tab",
                "requestId": req_id,
                "titleSubstrings": list(title_substrings),
                "excludeTitles": ["Advantage & Wonging Tracker", "Baccarat Tracker", "Companion Calibrator"],
                "preferLeft": bool(prefer_left),
                "bypassCache": bool(bypass_cache),
            }
        )
        try:
            await conn.send(payload)
        except Exception:
            self._ext_ack_waiters.pop(req_id, None)
            self.clients.discard(conn)
            self.ext_clients.discard(conn)
            return False, "ext_send_failed", None
        try:
            ack = await asyncio.wait_for(fut, timeout=timeout_sec)
        except asyncio.TimeoutError:
            self._ext_ack_waiters.pop(req_id, None)
            return False, "ext_ack_timeout", None
        if ack.get("ok"):
            return True, "ext_reload", ack
        reason = str(ack.get("reason") or "failed")
        return False, (reason if reason.startswith("ext_") else f"ext_{reason}"), ack

    async def broadcast_many(self, events: list[dict[str, Any]]) -> None:
        for ev in events:
            await self.broadcast(ev)
