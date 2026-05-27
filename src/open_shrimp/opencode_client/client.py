"""OpenCodeClient: per-conversation handle bound to one OpenCode session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from open_shrimp.opencode_client.errors import (
    CLIConnectionError,
    OpenCodeAuthError,
    ProcessError,
)
from open_shrimp.opencode_client.events import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    TextBlock,
)
from open_shrimp.opencode_client.options import OpenCodeOptions
from open_shrimp.opencode_client.process import OpenCodeServer
from open_shrimp.opencode_client.sse import EventBus, EventQueue

logger = logging.getLogger(__name__)


EVT_MESSAGE_PART_DELTA = "message.part.delta"
EVT_SESSION_IDLE = "session.idle"
EVT_SESSION_ERROR = "session.error"
EVT_SESSION_NEXT_STEP_FAILED = "session.next.step.failed"


_BUS_REGISTRY: dict[int, EventBus] = {}
_BUS_LOCK: asyncio.Lock | None = None


async def _get_bus(server: OpenCodeServer) -> EventBus:
    global _BUS_LOCK
    if _BUS_LOCK is None:
        _BUS_LOCK = asyncio.Lock()
    async with _BUS_LOCK:
        bus = _BUS_REGISTRY.get(id(server))
        if bus is None:
            bus = EventBus(server)
            await bus.start()
            _BUS_REGISTRY[id(server)] = bus
        return bus


async def _shutdown_buses() -> None:
    global _BUS_LOCK
    if _BUS_LOCK is None:
        _BUS_LOCK = asyncio.Lock()
    async with _BUS_LOCK:
        for bus in list(_BUS_REGISTRY.values()):
            await bus.stop()
        _BUS_REGISTRY.clear()


class OpenCodeClient:
    def __init__(self, options: OpenCodeOptions) -> None:
        self._options = options
        self._server: OpenCodeServer | None = None
        self._bus: EventBus | None = None
        self._events: EventQueue | None = None
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def __aenter__(self) -> "OpenCodeClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def connect(self) -> None:
        if self._server is not None:
            return
        self._server = await OpenCodeServer.get_or_start()
        self._bus = await _get_bus(self._server)
        self._http = httpx.AsyncClient(
            base_url=self._server.base_url,
            timeout=30.0,
            headers={"Authorization": self._server.auth_header},
        )
        try:
            if self._options.resume:
                self._session_id = self._options.resume
            else:
                self._session_id = await self._create_session()
            self._events = self._bus.subscribe(self._session_id)
        except BaseException:
            await self._http.aclose()
            self._http = None
            raise

    async def _create_session(self) -> str:
        assert self._http is not None
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        try:
            r = await self._http.post("/session", params=params, json={})
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"failed to create session: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code >= 400:
            raise ProcessError(
                f"POST /session returned {r.status_code}: {r.text[:300]}"
            )
        body = r.json()
        sid = body.get("id")
        if not sid:
            raise ProcessError(f"POST /session returned no id: {body!r}")
        return sid

    async def disconnect(self) -> None:
        if self._bus is not None and self._session_id is not None:
            self._bus.unsubscribe(self._session_id)
        self._events = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def query(self, prompt: str) -> None:
        if self._http is None or self._session_id is None:
            raise CLIConnectionError("OpenCodeClient.query called before connect()")
        body = {
            "model": {
                "providerID": self._options.provider,
                "modelID": self._options.model,
            },
            "parts": [{"type": "text", "text": prompt}],
        }
        try:
            r = await self._http.post(
                f"/session/{self._session_id}/prompt_async", json=body
            )
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"prompt_async failed: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code != 204:
            raise ProcessError(
                f"prompt_async returned {r.status_code}: {r.text[:300]}"
            )

    async def receive_response(self) -> AsyncIterator[Message]:
        if self._events is None or self._session_id is None:
            raise CLIConnectionError(
                "OpenCodeClient.receive_response called before connect()"
            )
        async for msg in _iter_response(
            self._events, self._session_id, self._options.query_timeout
        ):
            yield msg


async def _iter_response(
    queue: EventQueue,
    session_id: str,
    query_timeout: float,
) -> AsyncIterator[Message]:
    """Translate SSE events into wrapper messages until session.idle or timeout."""
    text_buffers: dict[str, list[str]] = {}
    part_order: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + query_timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise ProcessError("opencode serve query exceeded query_timeout")
        try:
            evt = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            raise ProcessError("opencode serve query exceeded query_timeout")
        except asyncio.CancelledError:
            raise ProcessError("opencode serve dropped the session")

        etype = evt.get("type", "")
        props = evt.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        if etype == EVT_SESSION_ERROR:
            raise ProcessError(_extract_error_message(props))

        if etype == EVT_SESSION_NEXT_STEP_FAILED:
            raise ProcessError(
                f"{EVT_SESSION_NEXT_STEP_FAILED}: {props.get('error')!r}"
            )

        if etype == EVT_SESSION_IDLE:
            for pid in part_order:
                text = "".join(text_buffers[pid])
                if text:
                    yield AssistantMessage(content=[TextBlock(text=text)])
            yield ResultMessage(session_id=session_id, is_error=False)
            return

        if etype == EVT_MESSAGE_PART_DELTA and props.get("field") == "text":
            part = props.get("part") or {}
            part_id = part.get("id") if isinstance(part, dict) else None
            if part_id is None:
                part_id = props.get("partID")
            delta = props.get("delta", "")
            if part_id is not None and isinstance(delta, str):
                if part_id not in text_buffers:
                    text_buffers[part_id] = []
                    part_order.append(part_id)
                text_buffers[part_id].append(delta)
            yield StreamEvent(event=evt)
            continue

        logger.debug("dropping event type=%s", etype)


def _extract_error_message(props: dict[str, Any]) -> str:
    err = props.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict):
            msg = data.get("message")
            if msg:
                return str(msg)
        name = err.get("name")
        if name:
            return str(name)
    return EVT_SESSION_ERROR
