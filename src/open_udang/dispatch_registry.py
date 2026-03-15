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

logger = logging.getLogger(__name__)

# The registered dispatch callback:
#   async def dispatch(prompt: str, chat_id: int) -> None
_dispatch_fn: Callable[[str, int], Awaitable[None]] | None = None


def register_dispatch(fn: Callable[[str, int], Awaitable[None]]) -> None:
    """Register the dispatch callback (called by the bot at startup)."""
    global _dispatch_fn
    _dispatch_fn = fn
    logger.info("Agent dispatch callback registered")


async def dispatch(prompt: str, chat_id: int) -> None:
    """Dispatch a prompt to the agent for the given chat.

    Raises RuntimeError if no callback has been registered yet.
    """
    if _dispatch_fn is None:
        raise RuntimeError("Agent dispatch not registered — bot may not be running")
    await _dispatch_fn(prompt, chat_id)
