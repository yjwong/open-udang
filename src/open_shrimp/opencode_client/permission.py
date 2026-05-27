"""Bridge OpenCode's async permission events to a synchronous can_use_tool callback.

Anthropic's ``canUseTool`` blocks the SDK while waiting for a decision.
OpenCode is event-based: ``permission.asked`` is fired on the SSE bus,
and the wrapper POSTs back to ``/permission/{id}/reply``. This bridge
joins the two shapes by:

1. Receiving events from the client's stream loop via ``observe()``.
2. Translating the OpenCode ``permission`` *category* (e.g. ``"edit"``)
   into a hooks ``tool_name`` (e.g. ``"Edit"``) — disambiguating via the
   in-flight ``ToolPart`` when needed.
3. Recovering the full ``tool_input`` dict either from a buffered
   ``ToolPart`` or by fetching the message on cache miss.
4. Awaiting ``can_use_tool`` and POSTing the reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from open_shrimp.opencode_client.events import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from open_shrimp.opencode_client.tool_names import (
    CATEGORY_TO_HOOKS,
    opencode_to_hooks,
)

logger = logging.getLogger(__name__)


CanUseToolCallback = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResult],
]

_TOOLPART_WAIT_TIMEOUT = 0.2  # seconds — bound the race with ToolPart caching.
_REPLIED_CACHE_MAX = 256  # FIFO eviction cap for the duplicate-asked guard.
_TERMINAL_STATUSES = frozenset({"completed", "error"})


class PermissionBridge:
    """One per-OpenCodeClient instance. Created in ``connect()``.

    The client forwards events to the bridge via ``observe()``:
    ``message.part.updated(part.type=tool)`` events update the
    ToolPart cache so ``permission.asked`` events can recover the
    ``tool_input`` dict that ``hooks.py`` expects. Permission events
    spawn a per-request background task so the client's stream loop
    isn't blocked while the user decides.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        can_use_tool: CanUseToolCallback,
        session_id: str,
    ) -> None:
        self._http = http
        self._can_use_tool = can_use_tool
        self._session_id = session_id
        self._tasks: set[asyncio.Task[None]] = set()
        # callID -> (tool_name, input_dict, message_id)
        self._tool_parts: dict[str, tuple[str, dict[str, Any], str]] = {}
        # callID -> Event signalling that ToolPart input has been buffered
        self._tool_part_events: dict[str, asyncio.Event] = {}
        # Requests we've already replied to (request_id only — the bridge
        # is bound to one session). Bounded FIFO via insertion-ordered dict.
        self._replied: dict[str, None] = {}

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    # --- internal -----------------------------------------------------

    def observe_tool_part(self, part: dict[str, Any]) -> None:
        """Update the ToolPart cache from a ``message.part.updated`` part.

        Caller has already verified ``part["type"] == "tool"``.
        """
        call_id = part.get("callID")
        if not call_id:
            return
        state = part.get("state") or {}
        status = state.get("status") if isinstance(state, dict) else None
        if status in _TERMINAL_STATUSES:
            self._tool_parts.pop(call_id, None)
            self._tool_part_events.pop(call_id, None)
            return
        tool_input: dict[str, Any] = {}
        if isinstance(state, dict):
            raw_input = state.get("input")
            if isinstance(raw_input, dict):
                tool_input = raw_input
        existing = self._tool_parts.get(call_id)
        if existing is not None and not tool_input and existing[1]:
            return
        self._tool_parts[call_id] = (
            part.get("tool") or "",
            tool_input,
            part.get("messageID") or "",
        )
        if tool_input:
            ev = self._tool_part_events.get(call_id)
            if ev is None:
                ev = asyncio.Event()
                self._tool_part_events[call_id] = ev
            ev.set()

    def observe_permission_asked(self, event: dict[str, Any]) -> None:
        """Dispatch a ``permission.asked`` event to a background task."""
        task = asyncio.create_task(
            self._handle_permission_asked(event),
            name="permission-bridge-asked",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_permission_asked(self, evt: dict[str, Any]) -> None:
        try:
            await self._do_handle_permission_asked(evt)
        except Exception:
            logger.exception("PermissionBridge handler crashed")
            await self._safe_reply(evt, reject="internal bridge error")

    async def _do_handle_permission_asked(self, evt: dict[str, Any]) -> None:
        props = evt.get("properties") or {}
        if not isinstance(props, dict):
            return
        request_id = props.get("id")
        if not request_id:
            logger.warning("permission.asked without id; dropping: %r", evt)
            return
        request_id = str(request_id)
        if request_id in self._replied:
            logger.debug(
                "permission.asked %s already handled; ignoring duplicate",
                request_id,
            )
            return
        self._mark_replied(request_id)

        session_id = props.get("sessionID") or self._session_id
        category = props.get("permission") or ""
        metadata = props.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        tool_ref = props.get("tool") or {}
        if not isinstance(tool_ref, dict):
            tool_ref = {}
        call_id = tool_ref.get("callID") or ""
        message_id = tool_ref.get("messageID") or ""

        tool_name, tool_input = await self._resolve_tool(
            category=str(category),
            call_id=str(call_id),
            message_id=str(message_id),
            session_id=str(session_id),
            metadata=metadata,
        )

        ctx = ToolPermissionContext(tool_use_id=call_id or request_id)

        logger.info(
            "Permission asked: category=%s tool=%s callID=%s",
            category, tool_name, call_id,
        )

        try:
            result = await self._can_use_tool(tool_name, tool_input, ctx)
        except Exception as exc:
            logger.exception(
                "can_use_tool raised for permission %s", request_id,
            )
            await self._safe_reply(
                evt, reject=f"internal error: {exc!r}",
            )
            return

        await self._send_reply(request_id, result)

    async def _resolve_tool(
        self,
        category: str,
        call_id: str,
        message_id: str,
        session_id: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Return (hooks_tool_name, tool_input) for a permission.asked event.

        Disambiguation order:
        1. The in-flight ToolPart cache (populated from
           ``message.part.updated``).
        2. ``GET /session/{sid}/message/{mid}`` fetch on cache miss.
        3. Static category → hooks-name fallback, with metadata as input.
        """
        tool_part = self._tool_parts.get(call_id)
        if tool_part is None and call_id:
            ev = self._tool_part_events.get(call_id)
            if ev is None:
                ev = asyncio.Event()
                self._tool_part_events[call_id] = ev
            try:
                await asyncio.wait_for(ev.wait(), timeout=_TOOLPART_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            tool_part = self._tool_parts.get(call_id)

        if tool_part is None and call_id and message_id:
            tool_part = await self._fetch_tool_part(
                session_id, message_id, call_id,
            )

        if tool_part is not None:
            opencode_name, tool_input, _ = tool_part
            return opencode_to_hooks(opencode_name), tool_input

        return CATEGORY_TO_HOOKS.get(category, category), dict(metadata)

    async def _fetch_tool_part(
        self,
        session_id: str,
        message_id: str,
        call_id: str,
    ) -> tuple[str, dict[str, Any], str] | None:
        try:
            r = await self._http.get(
                f"/session/{session_id}/message/{message_id}"
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to fetch message for permission lookup: %s", exc,
            )
            return None
        if r.status_code >= 400:
            logger.warning(
                "GET /session/%s/message/%s returned %d",
                session_id, message_id, r.status_code,
            )
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        parts = body.get("parts") if isinstance(body, dict) else None
        if not isinstance(parts, list):
            return None
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "tool":
                continue
            if part.get("callID") != call_id:
                continue
            tool_name = part.get("tool") or ""
            state = part.get("state") or {}
            tool_input: dict[str, Any] = {}
            if isinstance(state, dict):
                raw_input = state.get("input")
                if isinstance(raw_input, dict):
                    tool_input = raw_input
            result = (tool_name, tool_input, message_id)
            self._tool_parts[call_id] = result
            return result
        return None

    async def _send_reply(
        self, request_id: str, result: PermissionResult,
    ) -> None:
        body: dict[str, Any]
        if isinstance(result, PermissionResultAllow):
            updated_input = getattr(result, "updated_input", None)
            if updated_input is not None:
                logger.warning(
                    "PermissionResultAllow.updated_input is not supported by "
                    "OpenCode; dropping override for request %s",
                    request_id,
                )
            body = {"reply": "once"}
        elif isinstance(result, PermissionResultDeny):
            body = {"reply": "reject", "message": result.message}
        else:
            logger.warning(
                "can_use_tool returned unknown result %r; defaulting to reject",
                result,
            )
            body = {"reply": "reject", "message": "unknown permission result"}

        try:
            r = await self._http.post(
                f"/permission/{request_id}/reply", json=body,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "POST /permission/%s/reply failed: %s", request_id, exc,
            )
            self._replied.pop(request_id, None)
            return
        if r.status_code == 404:
            logger.debug(
                "permission %s already resolved (404)", request_id,
            )
            return
        if r.status_code >= 400:
            logger.warning(
                "POST /permission/%s/reply returned %d: %s",
                request_id, r.status_code, r.text[:200],
            )
            self._replied.pop(request_id, None)

    def _mark_replied(self, request_id: str) -> None:
        self._replied[request_id] = None
        while len(self._replied) > _REPLIED_CACHE_MAX:
            self._replied.pop(next(iter(self._replied)))

    async def _safe_reply(self, evt: dict[str, Any], *, reject: str) -> None:
        props = evt.get("properties") or {}
        request_id = (
            props.get("id") if isinstance(props, dict) else None
        )
        if not request_id:
            return
        await self._send_reply(
            str(request_id),
            PermissionResultDeny(message=reject),
        )
