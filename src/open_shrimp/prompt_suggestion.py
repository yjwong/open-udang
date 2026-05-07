"""Prompt suggestion plumbing.

Claude Code emits a ``prompt_suggestion`` frame on stream-json stdout a
short while after each turn's ``result`` frame.  The frame predicts what
the user is likely to type next and is normally surfaced in the CLI as
the input field's placeholder text (Tab to accept).

The Python ``claude-agent-sdk`` (0.1.65) does not expose this:
  * ``ClaudeAgentOptions`` has no ``prompt_suggestions`` field, so the
    flag never reaches the CLI's initialize control request and the
    feature stays off.
  * ``parse_message`` drops unknown message types, so even if the CLI
    did emit a suggestion the SDK consumer would never see it.

This module patches both gaps without forking the SDK:
  1. ``Query._send_control_request`` is wrapped so the ``initialize``
     request gains ``promptSuggestions: True``.
  2. ``SubprocessCLITransport.read_messages`` is wrapped so
     ``prompt_suggestion`` frames are diverted to a per-session
     callback registered by the streaming layer.

A small in-memory store maps short callback IDs to suggestion text so
that Telegram inline buttons (which cap callback_data at 64 bytes)
can ferry suggestions back when the user taps to accept them.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Type of a suggestion handler: receives the suggestion text and runs
# whatever side effect the consumer wants (e.g. edit a Telegram message
# to add an inline button).  Async so the consumer can await Telegram.
SuggestionHandler = Callable[[str], Awaitable[None]]

# Inline keyboard callback_data prefix used by both the producer
# (stream.py builds the button) and consumer (bot.py routes the tap).
CALLBACK_PREFIX = "suggest:"


# session_id -> handler.  Last-write wins: each new turn for a given
# session replaces the previous handler so a stale suggestion can never
# fire against a message the user has already moved past.
_handlers: dict[str, SuggestionHandler] = {}

# Short callback-id -> suggestion text.  Suggestions can exceed
# Telegram's 64-byte callback_data limit, so we round-trip via this
# dict.  Bounded; entries are popped on use, and old entries are
# evicted when either dict grows past _STORE_MAX.
_suggestion_store: dict[str, str] = {}
_STORE_MAX = 1000


def _evict_if_full(d: dict[str, Any]) -> None:
    """Drop the oldest half of *d* once it exceeds _STORE_MAX.

    Keeps unbounded growth in check for long-running bots where some
    sessions never receive a suggestion (so handlers are never popped)
    or a callback button is never tapped (so suggestions linger).
    """
    if len(d) > _STORE_MAX:
        for k in list(d.keys())[: _STORE_MAX // 2]:
            d.pop(k, None)


def register_handler(session_id: str, handler: SuggestionHandler) -> None:
    """Register *handler* to receive the next suggestion for *session_id*.

    Called by the streaming layer after a turn completes, with a
    closure that knows which Telegram message to edit.  Replacing an
    existing handler is fine and expected — only the latest turn's
    suggestion is interesting.
    """
    _evict_if_full(_handlers)
    _handlers[session_id] = handler


def unregister_handler(session_id: str) -> None:
    _handlers.pop(session_id, None)


def store_suggestion(text: str) -> str:
    """Stash *text* and return a short opaque id for callback_data.

    The id stays inside the ``CALLBACK_PREFIX<id>`` callback_data
    envelope and is popped by :func:`pop_suggestion` when the user
    taps the button.
    """
    _evict_if_full(_suggestion_store)
    suggest_id = secrets.token_urlsafe(8)
    _suggestion_store[suggest_id] = text
    return suggest_id


def pop_suggestion(suggest_id: str) -> str | None:
    return _suggestion_store.pop(suggest_id, None)


# ---------------------------------------------------------------------------
# SDK monkey-patches
# ---------------------------------------------------------------------------


_patches_applied = False


def install_patches() -> None:
    """Patch the SDK once.  Idempotent — safe to call from multiple modules.

    Also opts into the CLI's prompt-suggestion feature flag.  The CLI
    treats ``CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION`` as a kill-switch
    (``isEnvDefinedFalsy``); setting it to ``true`` ensures the feature
    is active even if a host environment overrode it elsewhere.
    """
    global _patches_applied
    if _patches_applied:
        return

    os.environ.setdefault("CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION", "true")

    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    # ------- patch 1: inject promptSuggestions into initialize request -------
    _orig_send_control = Query._send_control_request

    async def _patched_send_control(
        self: Any, request: dict[str, Any], timeout: float | None = None
    ) -> Any:
        if request.get("subtype") == "initialize":
            request["promptSuggestions"] = True
            logger.debug("injected promptSuggestions=True into init request")
        # The SDK's signature accepts an optional timeout; preserve it.
        if timeout is None:
            return await _orig_send_control(self, request)
        return await _orig_send_control(self, request, timeout=timeout)

    Query._send_control_request = _patched_send_control  # type: ignore[method-assign]

    # ------- patch 2: divert prompt_suggestion frames to handlers -------
    _orig_read = SubprocessCLITransport.read_messages

    async def _patched_read(self: Any) -> Any:
        async for msg in _orig_read(self):
            if isinstance(msg, dict) and msg.get("type") == "prompt_suggestion":
                session_id = msg.get("session_id")
                suggestion = msg.get("suggestion")
                if session_id and suggestion:
                    handler = _handlers.pop(session_id, None)
                    if handler is not None:
                        import asyncio

                        asyncio.create_task(_run_handler(handler, suggestion))
            # Yield every frame downstream — the SDK parser drops
            # unknown types as forward-compat, but yielding (instead
            # of skipping) keeps the iterator's flow identical to the
            # unpatched path.
            yield msg

    SubprocessCLITransport.read_messages = _patched_read  # type: ignore[method-assign]

    _patches_applied = True
    logger.info("prompt_suggestion patches installed")


async def _run_handler(handler: SuggestionHandler, suggestion: str) -> None:
    try:
        await handler(suggestion)
    except Exception:
        logger.exception("prompt_suggestion handler failed")
