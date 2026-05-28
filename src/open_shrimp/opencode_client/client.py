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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from open_shrimp.opencode_client.options import OpenCodeOptions
from open_shrimp.opencode_client.permission import PermissionBridge
from open_shrimp.opencode_client.process import OpenCodeServer
from open_shrimp.opencode_client.sse import EventBus, EventQueue
from open_shrimp.opencode_client.tool_names import (
    OPENCODE_PERMISSION_CATEGORIES,
    opencode_to_hooks,
)

logger = logging.getLogger(__name__)


EVT_MESSAGE_PART_DELTA = "message.part.delta"
EVT_MESSAGE_PART_UPDATED = "message.part.updated"
EVT_MESSAGE_UPDATED = "message.updated"
EVT_PERMISSION_ASKED = "permission.asked"
EVT_SESSION_IDLE = "session.idle"
EVT_SESSION_ERROR = "session.error"

_TOKEN_KEYS = ("input", "output", "reasoning")

_TOOL_STATUS_PENDING = "pending"
_TOOL_STATUS_RUNNING = "running"
_TOOL_STATUS_COMPLETED = "completed"
_TOOL_STATUS_ERROR = "error"
_TOOL_STATUS_IN_FLIGHT = frozenset({_TOOL_STATUS_PENDING, _TOOL_STATUS_RUNNING})

_PART_TYPE_TOOL = "tool"
_PART_TYPE_REASONING = "reasoning"

_MUTATING_OPENCODE_PERMS = frozenset({"edit", "write", "apply_patch"})
_ALWAYS_ALLOWED_OPENCODE_PERMS = frozenset({"todowrite"})


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
        self._bridge: PermissionBridge | None = None
        self._permission_rules: list[dict[str, Any]] = []

    async def __aenter__(self) -> "OpenCodeClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def is_alive(self) -> bool:
        """True if the underlying ``opencode serve`` process is healthy."""
        server = self._server
        if server is None:
            return False
        proc = getattr(server, "proc", None)
        if proc is None:
            return False
        return getattr(proc, "returncode", None) is None

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
                try:
                    await self._patch_permission_rules(
                        self._build_initial_rules()
                    )
                except CLIConnectionError as exc:
                    if _is_not_found_error(exc):
                        logger.warning(
                            "Resume target %s missing; starting fresh session",
                            self._session_id,
                        )
                        self._session_id = await self._create_session()
                    else:
                        raise
            else:
                self._session_id = await self._create_session()
            assert self._session_id is not None
            self._events = self._bus.subscribe(self._session_id)
            if self._options.can_use_tool is not None:
                self._bridge = PermissionBridge(
                    http=self._http,
                    can_use_tool=self._options.can_use_tool,
                    session_id=self._session_id,
                )
        except BaseException:
            await self._http.aclose()
            self._http = None
            raise

    async def _create_session(self) -> str:
        assert self._http is not None
        params: dict[str, str] = {}
        if self._options.cwd:
            params["directory"] = self._options.cwd
        rules = self._build_initial_rules()
        self._permission_rules = list(rules)
        body: dict[str, Any] = {}
        if rules:
            body["permission"] = rules
        try:
            r = await self._http.post("/session", params=params, json=body)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"failed to create session: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code >= 400:
            raise ProcessError(
                f"POST /session returned {r.status_code}: {r.text[:300]}"
            )
        payload = r.json()
        sid = payload.get("id")
        if not sid:
            raise ProcessError(f"POST /session returned no id: {payload!r}")
        return sid

    def _build_initial_rules(self) -> list[dict[str, Any]]:
        """Construct the initial permission ruleset for this session.

        Order matters: OpenCode's evaluator picks the LAST matching
        rule, so the ask-baseline goes first and user allows go after.
        """
        rules: list[dict[str, Any]] = [
            {"permission": "*", "pattern": "*", "action": "ask"},
        ]
        for category in OPENCODE_PERMISSION_CATEGORIES:
            rules.append(
                {"permission": category, "pattern": "*", "action": "ask"}
            )
        for permission in sorted(_ALWAYS_ALLOWED_OPENCODE_PERMS):
            rules.append(
                {"permission": permission, "pattern": "*", "action": "allow"}
            )
        rules.extend(self._rules_from_allowed_tools())
        rules.extend(self._rules_from_add_dirs())
        return rules

    def _rules_from_allowed_tools(self) -> list[dict[str, Any]]:
        """Translate ``allowed_tools`` entries to OpenCode allow rules.

        Mutating tools (edit/write/apply_patch) are intentionally skipped —
        they always go through ``can_use_tool`` unless "accept all edits"
        is toggled on, which routes through ``update_permission_rules``.
        """
        out: list[dict[str, Any]] = []
        tools = self._options.allowed_tools or []
        for entry in tools:
            if not isinstance(entry, str):
                continue
            permission, pattern = _parse_allowed_tool(entry)
            if permission is None:
                continue
            if permission in _MUTATING_OPENCODE_PERMS:
                continue
            out.append(
                {
                    "permission": permission,
                    "pattern": pattern or "*",
                    "action": "allow",
                }
            )
        return out

    def _rules_from_add_dirs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        dirs: list[str] = []
        if self._options.cwd:
            dirs.append(self._options.cwd)
        if self._options.add_dirs:
            dirs.extend(self._options.add_dirs)
        for d in dirs:
            if not d:
                continue
            pattern = d.rstrip("/") + "/*"
            out.append(
                {
                    "permission": "external_directory",
                    "pattern": pattern,
                    "action": "allow",
                }
            )
        return out

    async def disconnect(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None
        if self._bus is not None and self._session_id is not None:
            self._bus.unsubscribe(self._session_id)
        self._events = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def query(self, prompt: str) -> None:
        if self._http is None or self._session_id is None:
            raise CLIConnectionError("OpenCodeClient.query called before connect()")
        body: dict[str, Any] = {
            "model": {
                "providerID": self._options.provider,
                "modelID": self._options.model,
            },
            "parts": [{"type": "text", "text": prompt}],
        }
        if self._options.system_prompt is not None:
            body["system"] = _coerce_system_prompt(self._options.system_prompt)
        if self._options.effort is not None:
            body["variant"] = self._options.effort
        try:
            r = await self._http.post(
                f"/session/{self._session_id}/prompt_async", json=body
            )
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"prompt_async failed: {exc}") from exc
        if r.status_code == 401:
            raise OpenCodeAuthError("opencode serve rejected our credentials")
        if r.status_code == 404:
            raise CLIConnectionError(
                f"prompt_async returned 404 for session {self._session_id}"
            )
        if r.status_code != 204:
            raise ProcessError(
                f"prompt_async returned {r.status_code}: {r.text[:300]}"
            )

    async def stop_task(self, task_id: str) -> None:
        """Best-effort background-task stop.

        OpenCode does not expose a per-task abort endpoint, so this
        currently falls back to a full-session interrupt.
        """
        logger.warning(
            "stop_task is not implemented for OpenCode (task_id=%s); "
            "falling back to interrupt()", task_id,
        )
        await self.interrupt()

    async def interrupt(self) -> None:
        """Abort the in-flight turn for this session.

        Maps to OpenCode's ``POST /session/{id}/abort`` endpoint.
        """
        if self._http is None or self._session_id is None:
            return
        try:
            await self._http.post(f"/session/{self._session_id}/abort")
        except httpx.HTTPError as exc:
            logger.warning("interrupt: POST /abort failed: %s", exc)

    async def update_permission_rules(
        self, rules: list[dict[str, Any]],
    ) -> None:
        """Replace the session's permission ruleset.

        Used when the user toggles "accept all edits" — passes the
        rebuilt ruleset to ``PATCH /session/{id}``.
        """
        self._permission_rules = list(rules)
        await self._patch_permission_rules(rules)

    async def _patch_permission_rules(
        self, rules: list[dict[str, Any]],
    ) -> None:
        if self._http is None or self._session_id is None:
            return
        try:
            r = await self._http.patch(
                f"/session/{self._session_id}",
                json={"permission": rules},
            )
        except httpx.HTTPError as exc:
            raise CLIConnectionError(
                f"PATCH /session/{self._session_id} failed: {exc}"
            ) from exc
        if r.status_code == 404:
            raise CLIConnectionError(
                f"PATCH /session/{self._session_id} returned 404"
            )
        if r.status_code >= 400:
            raise ProcessError(
                f"PATCH /session/{self._session_id} returned "
                f"{r.status_code}: {r.text[:300]}"
            )

    @property
    def permission_rules(self) -> list[dict[str, Any]]:
        """Current session permission ruleset (most recent build)."""
        return list(self._permission_rules)

    async def receive_response(self) -> AsyncIterator[Message]:
        if self._events is None or self._session_id is None:
            raise CLIConnectionError(
                "OpenCodeClient.receive_response called before connect()"
            )
        async for msg in _iter_response(
            self._events,
            self._session_id,
            self._bridge,
        ):
            yield msg


def _parse_allowed_tool(entry: str) -> tuple[str | None, str | None]:
    """Parse an ``allowed_tools`` entry into (permission, pattern).

    Accepts both the OpenCode wire form (``bash``, ``bash(git *)``) and
    the capitalised hooks form (``Bash``, ``Bash(git *)``). Lowercases
    everything so the result is an OpenCode permission name.
    """
    text = entry.strip()
    if not text:
        return None, None
    pattern: str | None = None
    if "(" in text and text.endswith(")"):
        head, _, tail = text.partition("(")
        text = head.strip()
        pattern = tail[:-1].strip() or None
    # Treat MCP qualified names (mcp__server__tool, server_tool) as
    # opaque permission names — no translation.
    if text.startswith("mcp__"):
        return text, pattern
    if text.startswith("_"):
        return text, pattern
    lowered = text.lower()
    return lowered, pattern


def _coerce_system_prompt(value: Any) -> str:
    """Normalise ``options.system_prompt`` into a string for OpenCode.

    Anthropic accepts a string OR a ``{"type": "preset", "preset": …,
    "append": …}`` dict. OpenCode's ``system`` field is a plain
    appendable string, so we pull out the ``append`` text when given
    the preset shape and ignore the rest.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        append = value.get("append")
        if isinstance(append, str):
            return append
    return ""


def _is_not_found_error(exc: BaseException) -> bool:
    """Heuristic: does an exception suggest the OpenCode session is gone?"""
    msg = str(exc)
    return "404" in msg


def _resolve_part_id(props: dict[str, Any]) -> str | None:
    """Extract a part id from an SSE event's ``properties``.

    OpenCode publishes ``message.part.delta`` with the id either nested
    as ``part.id`` or flat as ``partID``; older snapshots use one, newer
    ones the other. Callers should treat them interchangeably.
    """
    part = props.get("part")
    if isinstance(part, dict):
        pid = part.get("id")
        if isinstance(pid, str):
            return pid
    pid = props.get("partID")
    return pid if isinstance(pid, str) else None


async def _iter_response(
    queue: EventQueue,
    session_id: str,
    bridge: PermissionBridge | None,
) -> AsyncIterator[Message]:
    """Translate SSE events into wrapper messages until session.idle."""
    text_buffers: dict[str, list[str]] = {}
    part_order: list[str] = []
    tool_use_emitted: set[str] = set()
    tool_result_emitted: set[str] = set()
    # Reasoning parts surface as message.part.delta with field="text", same as
    # real text parts (opencode processor.ts emits updatePartDelta with
    # field:"text" for reasoning-delta). The delta payload itself doesn't
    # carry the part type, so we learn it from message.part.updated and
    # drop matching deltas to keep thinking traces out of Telegram.
    reasoning_part_ids: set[str] = set()
    loop = asyncio.get_running_loop()
    turn_start_ms = int(loop.time() * 1000)

    # Per-turn usage accumulation. OpenCode emits a stream of
    # ``message.updated`` events per assistant message; we treat each
    # unique assistant ``info.id`` as one "step" and finalise the
    # step's tokens/cost/error exactly once (on first sight of
    # ``finish`` or ``error`` in the info payload).
    model_usage: dict[str, dict[str, Any]] = {}
    total_cost_usd = 0.0
    errors: list[dict[str, Any]] = []
    seen_step_ids: set[str] = set()      # any assistant id we've seen at all
    finalised_steps: set[str] = set()    # ids whose tokens/error we've folded

    def _flush_text() -> list[AssistantMessage]:
        out: list[AssistantMessage] = []
        for pid in part_order:
            text = "".join(text_buffers[pid])
            if text:
                out.append(
                    AssistantMessage(content=[TextBlock(text=text)])
                )
        text_buffers.clear()
        part_order.clear()
        return out

    def _flush_step(
        usage: dict[str, Any] | None,
        error: str | None,
    ) -> list[AssistantMessage]:
        """Flush buffered text and attach the step's usage/error.

        Usage/error rides on the *final* AssistantMessage of the step.
        When the step produced no text we still emit an empty
        AssistantMessage so per-turn UI in ``stream.py`` (which reads
        ``event.usage``) sees the update.
        """
        out = _flush_text()
        if usage is None and error is None:
            return out
        if not out:
            out.append(AssistantMessage(content=[]))
        out[-1].usage = usage
        out[-1].error = error
        return out

    while True:
        try:
            evt = await queue.get()
        except asyncio.CancelledError:
            raise ProcessError("opencode serve dropped the session")

        etype = evt.get("type", "")
        props = evt.get("properties") or {}
        if not isinstance(props, dict):
            props = {}


        if etype == EVT_SESSION_ERROR:
            raise ProcessError(_extract_error_message(props))

        if etype == EVT_MESSAGE_UPDATED:
            info = props.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            step_id = info.get("id")
            if not isinstance(step_id, str):
                continue
            seen_step_ids.add(step_id)
            if step_id in finalised_steps:
                continue

            err = info.get("error")
            err_message: str | None = None
            if isinstance(err, dict):
                raw = err.get("message")
                if isinstance(raw, str) and raw:
                    err_message = raw

            finish = info.get("finish")
            if err_message is None and not isinstance(finish, str):
                # Step is still in flight — wait for finish/error.
                continue

            finalised_steps.add(step_id)
            model_id = info.get("modelID")
            tokens = info.get("tokens")
            tokens = tokens if isinstance(tokens, dict) else None
            cost = info.get("cost")
            cost = float(cost) if isinstance(cost, (int, float)) else 0.0
            if err_message is None:
                total_cost_usd += cost
                _fold_into_model_usage(
                    model_usage,
                    model_id if isinstance(model_id, str) else None,
                    tokens, cost,
                )
                for msg in _flush_step(usage=tokens, error=None):
                    yield msg
            else:
                errors.append(
                    {
                        "message": err_message,
                        "when": (info.get("time") or {}).get("completed"),
                    }
                )
                for msg in _flush_step(usage=None, error=err_message):
                    yield msg
            continue

        if etype == EVT_SESSION_IDLE:
            for msg in _flush_text():
                yield msg
            yield ResultMessage(
                session_id=session_id,
                total_cost_usd=total_cost_usd,
                usage=_aggregate_tokens(model_usage),
                model_usage=model_usage,
                num_steps=len(seen_step_ids),
                duration_ms=int(loop.time() * 1000) - turn_start_ms,
                errors=errors,
                is_error=bool(errors),
            )
            return

        if etype == EVT_MESSAGE_PART_DELTA and props.get("field") == "text":
            part_id = _resolve_part_id(props)
            if part_id in reasoning_part_ids:
                continue
            delta = props.get("delta", "")
            if part_id is not None and isinstance(delta, str):
                if part_id not in text_buffers:
                    text_buffers[part_id] = []
                    part_order.append(part_id)
                text_buffers[part_id].append(delta)
            yield StreamEvent(event=evt)
            continue

        if etype == EVT_MESSAGE_PART_UPDATED:
            part = props.get("part") or {}
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type == _PART_TYPE_REASONING:
                    pid = part.get("id")
                    if isinstance(pid, str):
                        reasoning_part_ids.add(pid)
                elif part_type == _PART_TYPE_TOOL:
                    if bridge is not None:
                        bridge.observe_tool_part(part)
                    for msg in _toolpart_messages(
                        part, tool_use_emitted, tool_result_emitted,
                        flush_text=_flush_text,
                    ):
                        yield msg
            continue

        if etype == EVT_PERMISSION_ASKED:
            if bridge is not None:
                bridge.observe_permission_asked(evt)
            continue

        logger.debug("dropping event type=%s", etype)


def _new_token_bucket() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }


def _add_tokens(dest: dict[str, Any], src: dict[str, Any]) -> None:
    """Add ``src``'s token fields into ``dest`` in place.

    Both dicts use the OpenCode-native shape produced by
    ``_new_token_bucket``. ``src`` may also be raw wire ``tokens`` —
    untrusted, so each scalar is type-guarded.
    """
    for key in _TOKEN_KEYS:
        val = src.get(key)
        if isinstance(val, (int, float)):
            dest[key] += int(val)
    cache = src.get("cache")
    if isinstance(cache, dict):
        dest_cache = dest["cache"]
        for sub in ("read", "write"):
            val = cache.get(sub)
            if isinstance(val, (int, float)):
                dest_cache[sub] += int(val)


def _fold_into_model_usage(
    model_usage: dict[str, dict[str, Any]],
    model_id: str | None,
    tokens: dict[str, Any] | None,
    cost: float,
) -> None:
    """Accumulate one step's tokens/cost into the per-model bucket."""
    if model_id is None:
        return
    bucket = model_usage.get(model_id)
    if bucket is None:
        bucket = _new_token_bucket()
        bucket["cost"] = 0.0
        model_usage[model_id] = bucket
    if tokens is not None:
        _add_tokens(bucket, tokens)
    bucket["cost"] += cost


def _aggregate_tokens(
    model_usage: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Sum per-model token buckets into a single OpenCode-native dict."""
    total = _new_token_bucket()
    for bucket in model_usage.values():
        _add_tokens(total, bucket)
    return total


def _toolpart_messages(
    part: dict[str, Any],
    tool_use_emitted: set[str],
    tool_result_emitted: set[str],
    *,
    flush_text,
) -> list[Message]:
    """Synthesise ToolUseBlock / ToolResultBlock messages from a ToolPart.

    Emit rules:
    * First sight of ``pending``/``running`` with non-empty ``input`` →
      ``AssistantMessage([ToolUseBlock])``.
    * First sight of ``completed`` → ``UserMessage([ToolResultBlock])``.
    * First sight of ``error`` → ``UserMessage([ToolResultBlock(is_error=True)])``.
    * Anything else is a repeat and dropped.
    """
    raw_call_id = part.get("callID")
    if not raw_call_id:
        return []
    call_id = str(raw_call_id)
    state = part.get("state") or {}
    if not isinstance(state, dict):
        return []
    status = state.get("status")

    if status in _TOOL_STATUS_IN_FLIGHT:
        if call_id in tool_use_emitted:
            return []
        raw_input = state.get("input")
        if not isinstance(raw_input, dict) or not raw_input:
            return []
        out: list[Message] = list(flush_text())
        out.append(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id=call_id,
                        name=opencode_to_hooks(part.get("tool") or ""),
                        input=raw_input,
                    )
                ]
            )
        )
        tool_use_emitted.add(call_id)
        return out

    if status == _TOOL_STATUS_COMPLETED:
        if call_id in tool_result_emitted:
            return []
        output = state.get("output")
        tool_result_emitted.add(call_id)
        return [
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=call_id,
                        content=output if output is not None else "",
                        is_error=False,
                    )
                ]
            )
        ]

    if status == _TOOL_STATUS_ERROR:
        if call_id in tool_result_emitted:
            return []
        err = state.get("error")
        tool_result_emitted.add(call_id)
        return [
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=call_id,
                        content=err if err is not None else "tool error",
                        is_error=True,
                    )
                ]
            )
        ]

    return []


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
