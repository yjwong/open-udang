"""Shared dispatch registry for cross-component communication.

The Telegram bot registers a dispatch callback at startup.  Other
components (e.g. the review API HTTP server) can call ``dispatch()``
to send a prompt to the agent for a given chat.

This avoids coupling the review API directly to the bot's
``Application`` object or handler internals.
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

from open_shrimp.db import ChatScope

logger = logging.getLogger(__name__)

# The registered dispatch callback:
#   async def dispatch(prompt: str, scope: ChatScope, placeholder: str | None) -> None
_dispatch_fn: Callable[[str, ChatScope, str | None], Awaitable[None]] | None = None
_parent_notification_fn: (
    Callable[[ChatScope, int, bool], Awaitable[None]] | None
) = None


def register_dispatch(fn: Callable[[str, ChatScope, str | None], Awaitable[None]]) -> None:
    """Register the dispatch callback (called by the bot at startup)."""
    global _dispatch_fn
    _dispatch_fn = fn
    logger.info("Agent dispatch callback registered")


def register_parent_notification_wakeup(
    fn: Callable[[ChatScope, int, bool], Awaitable[None]],
) -> None:
    """Register the callback that drives idle parent sessions after agent tasks."""
    global _parent_notification_fn
    _parent_notification_fn = fn
    logger.info("Agent parent-notification wakeup callback registered")


async def dispatch(
    prompt: str,
    chat_id: int,
    thread_id: int | None = None,
    *,
    placeholder: str | None = None,
) -> None:
    """Dispatch a prompt to the agent for the given chat.

    Wraps ``chat_id`` and optional ``thread_id`` in a ``ChatScope``.
    If *placeholder* is given, the bot sends it as a message before
    starting the agent task, giving the user immediate feedback.

    Raises RuntimeError if no callback has been registered yet.
    """
    if _dispatch_fn is None:
        raise RuntimeError("Agent dispatch not registered — bot may not be running")
    await _dispatch_fn(prompt, ChatScope(chat_id, thread_id), placeholder)


async def wake_parent_for_agent_notification(
    scope: ChatScope,
    *,
    user_id: int = 0,
    is_private_chat: bool = True,
) -> None:
    """Drive an idle parent session so queued Agent results are processed."""
    if _parent_notification_fn is None:
        logger.debug("Parent-notification wakeup callback is not registered")
        return
    await _parent_notification_fn(scope, user_id, is_private_chat)
