"""SSE event bus with per-session demultiplexing."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from open_shrimp.opencode_client.errors import CLIConnectionError
from open_shrimp.opencode_client.process import OpenCodeServer

logger = logging.getLogger("opencode.sse")


_DEFAULT_QUEUE_SIZE = 1024
_CONNECT_TIMEOUT = 5.0
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0

EVT_SERVER_CONNECTED = "server.connected"


class EventQueueClosed(Exception):
    """Raised when a local subscriber queue is closed intentionally."""


class EventQueue:
    """Per-session bounded queue. Drops oldest on overflow."""

    def __init__(self, session_id: str, maxsize: int = _DEFAULT_QUEUE_SIZE) -> None:
        self.session_id = session_id
        self._q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._overflow_logged = False
        self._closed = False

    def put_nowait(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if not self._overflow_logged:
                logger.warning(
                    "event queue for session %s overflowed (maxsize=%d); "
                    "dropping oldest events",
                    self.session_id,
                    self._maxsize,
                )
                self._overflow_logged = True
            try:
                self._q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def get(self) -> dict[str, Any]:
        evt = await self._q.get()
        if evt is None:
            raise EventQueueClosed
        return evt

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._q.put_nowait(None)
        except asyncio.QueueFull:
            pass


class EventBus:
    """One long-lived `/event` connection, demultiplexed by sessionID."""

    def __init__(
        self,
        server: OpenCodeServer,
        *,
        http_client: httpx.AsyncClient | None = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._server = server
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=server.base_url,
            timeout=None,
            headers={"Authorization": server.auth_header},
        )
        self._queue_size = queue_size
        self._subscribers: dict[str, EventQueue] = {}
        self._broadcast = EventQueue("__broadcast__", maxsize=queue_size)
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._reader_task is not None:
                return
            self._reader_task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise CLIConnectionError(
                f"opencode serve did not emit server.connected within {_CONNECT_TIMEOUT}s"
            ) from exc

    async def stop(self) -> None:
        self._stop.set()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("SSE reader task raised on stop: %s", exc)
            self._reader_task = None
        for q in list(self._subscribers.values()):
            q.close()
        self._subscribers.clear()
        self._broadcast.close()
        if self._owns_client:
            await self._http.aclose()

    def subscribe(self, session_id: str) -> EventQueue:
        q = self._subscribers.get(session_id)
        if q is None:
            q = EventQueue(session_id, maxsize=self._queue_size)
            self._subscribers[session_id] = q
        return q

    def unsubscribe(self, session_id: str) -> None:
        q = self._subscribers.pop(session_id, None)
        if q is not None:
            q.close()

    async def _run(self) -> None:
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                await self._read_stream()
                if self._stop.is_set():
                    return
                logger.warning("SSE stream ended; reconnecting in %.1fs", backoff)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._stop.is_set():
                    return
                logger.warning("SSE stream error (%s); reconnecting in %.1fs", exc, backoff)
            # Jittered sleep: avoid thundering-herd if many clients reconnect together.
            sleep_for = backoff * random.uniform(0.5, 1.5)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _read_stream(self) -> None:
        async with self._http.stream("GET", "/event") as r:
            r.raise_for_status()
            async for event in _sse_events(r):
                if self._stop.is_set():
                    return
                self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype == EVT_SERVER_CONNECTED and not self._ready.is_set():
            self._ready.set()
        props = event.get("properties") or {}
        sid = props.get("sessionID") if isinstance(props, dict) else None
        target = self._subscribers.get(sid) if sid else None
        if target is not None:
            target.put_nowait(event)
        else:
            self._broadcast.put_nowait(event)


async def _sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    buf = ""
    async for chunk in response.aiter_text():
        buf += chunk
        while "\n\n" in buf:
            raw_event, buf = buf.split("\n\n", 1)
            data_lines = [
                ln[5:].lstrip()
                for ln in raw_event.splitlines()
                if ln.startswith("data:")
            ]
            if not data_lines:
                continue
            try:
                yield json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                logger.debug("ignoring un-parseable SSE event: %r", raw_event[:200])
                continue
