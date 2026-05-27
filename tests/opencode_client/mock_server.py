"""Scriptable Starlette mock of `opencode serve` for unit tests.

Tests script the SSE replay per session via `MockOpenCode.scripts[sid]`;
`properties.sessionID` is auto-filled if missing.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


class MockOpenCode:
    """A scriptable mock OpenCode server."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[dict[str, Any]]] = {}
        # Optional per-session response delay before kicking the script.
        self.delays: dict[str, float] = {}
        # Records POSTs for assertions.
        self.created_sessions: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        # Records permission replies for assertions.
        self.permission_replies: list[dict[str, Any]] = []
        # Records session patches (e.g. update_permission_rules).
        self.patched_sessions: list[dict[str, Any]] = []
        # Records aborts.
        self.aborted_sessions: list[str] = []
        # Per-session "stored" messages used by GET /session/{sid}/message/{mid}.
        # Tests script this when they need the bridge's message-fetch
        # fallback to find a matching ToolPart.
        self.messages: dict[tuple[str, str], dict[str, Any]] = {}

        # Each /event subscriber gets its own queue. _subscribers is a list
        # of asyncio.Queue[dict | None]. None signals end-of-stream.
        self._subscribers: list[asyncio.Queue[dict[str, Any] | None]] = []
        # Whether new subscribers should receive the initial server.connected.
        self.send_initial_connected = True

        self.app = Starlette(
            routes=[
                Route("/event", self._event_stream),
                Route("/session", self._create_session, methods=["POST"]),
                Route(
                    "/session/{sid}",
                    self._patch_session,
                    methods=["PATCH"],
                ),
                Route(
                    "/session/{sid}/prompt_async",
                    self._prompt_async,
                    methods=["POST"],
                ),
                Route(
                    "/session/{sid}/abort",
                    self._abort_session,
                    methods=["POST"],
                ),
                Route(
                    "/session/{sid}/message/{mid}",
                    self._get_message,
                    methods=["GET"],
                ),
                Route(
                    "/permission/{rid}/reply",
                    self._permission_reply,
                    methods=["POST"],
                ),
            ]
        )

    # --- helpers used by tests ----------------------------------------

    def script(self, session_id: str, events: list[dict[str, Any]]) -> None:
        """Set the script of events to replay when this session is prompted."""
        self.scripts[session_id] = list(events)

    def broadcast(self, event: dict[str, Any]) -> None:
        """Push an event to every active subscriber (no demux)."""
        for q in self._subscribers:
            q.put_nowait(event)

    async def disconnect_all(self) -> None:
        """Close every active SSE stream (simulates a server crash)."""
        for q in self._subscribers:
            q.put_nowait(None)

    # --- HTTP handlers ------------------------------------------------

    async def _event_stream(self, request: Request) -> Response:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribers.append(q)
        if self.send_initial_connected:
            q.put_nowait({"type": "server.connected", "properties": {}})

        async def gen() -> AsyncIterator[bytes]:
            try:
                while True:
                    evt = await q.get()
                    if evt is None:
                        return
                    payload = json.dumps(evt)
                    yield f"data: {payload}\n\n".encode("utf-8")
            finally:
                if q in self._subscribers:
                    self._subscribers.remove(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _create_session(self, request: Request) -> Response:
        body = await request.body()
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        sid = secrets.token_hex(8)
        self.created_sessions.append(
            {"id": sid, "body": data, "params": dict(request.query_params)}
        )
        return JSONResponse({"id": sid})

    async def _prompt_async(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        self.prompts.append({"session_id": sid, "body": body})

        script = self.scripts.get(sid, [])
        delay = self.delays.get(sid, 0.0)

        async def replay() -> None:
            if delay:
                await asyncio.sleep(delay)
            for evt in script:
                evt = _ensure_session_id(evt, sid)
                for q in self._subscribers:
                    q.put_nowait(evt)
                await asyncio.sleep(0)

        asyncio.create_task(replay())
        return Response(status_code=204)

    async def _patch_session(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        self.patched_sessions.append({"session_id": sid, "body": body})
        return JSONResponse({"id": sid})

    async def _abort_session(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        self.aborted_sessions.append(sid)
        return Response(status_code=204)

    async def _get_message(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        mid = request.path_params["mid"]
        key = (sid, mid)
        if key in self.messages:
            return JSONResponse(self.messages[key])
        return Response(status_code=404)

    async def _permission_reply(self, request: Request) -> Response:
        rid = request.path_params["rid"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        self.permission_replies.append({"request_id": rid, "body": body})
        return JSONResponse({"ok": True})


def _ensure_session_id(evt: dict[str, Any], sid: str) -> dict[str, Any]:
    """Add properties.sessionID if the test didn't bother to."""
    out = dict(evt)
    props = dict(out.get("properties") or {})
    props.setdefault("sessionID", sid)
    out["properties"] = props
    return out


def text_delta(part_id: str, delta: str) -> dict[str, Any]:
    return {
        "type": "message.part.delta",
        "properties": {
            "field": "text",
            "delta": delta,
            "part": {"id": part_id, "type": "text"},
        },
    }


def session_idle() -> dict[str, Any]:
    return {"type": "session.idle", "properties": {}}


def session_error(message: str) -> dict[str, Any]:
    return {
        "type": "session.error",
        "properties": {
            "error": {"name": "TestError", "data": {"message": message}},
        },
    }


def tool_part_event(
    call_id: str,
    tool: str,
    status: str,
    *,
    message_id: str = "msg_1",
    tool_input: dict[str, Any] | None = None,
    output: str | None = None,
    error: str | None = None,
    part_id: str | None = None,
) -> dict[str, Any]:
    """Build a ``message.part.updated`` event for a tool part.

    ``tool_input`` is required for ``running``/``completed``/``error``
    statuses (mirrors the OpenCode schema); ``pending`` parts may omit it.
    """
    state: dict[str, Any] = {"status": status}
    if tool_input is not None:
        state["input"] = tool_input
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    part: dict[str, Any] = {
        "type": "tool",
        "id": part_id or f"prt_{call_id}",
        "messageID": message_id,
        "tool": tool,
        "callID": call_id,
        "state": state,
    }
    return {
        "type": "message.part.updated",
        "properties": {
            "messageID": message_id,
            "part": part,
        },
    }


def permission_asked(
    request_id: str,
    category: str,
    *,
    call_id: str = "call_1",
    message_id: str = "msg_1",
    metadata: dict[str, Any] | None = None,
    patterns: list[str] | None = None,
    always: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ``permission.asked`` event matching the OpenCode wire shape."""
    return {
        "type": "permission.asked",
        "properties": {
            "id": request_id,
            "permission": category,
            "patterns": patterns or [],
            "metadata": metadata or {},
            "always": always or [],
            "tool": {"messageID": message_id, "callID": call_id},
        },
    }
