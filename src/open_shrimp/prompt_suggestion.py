"""Prompt suggestion generation and Telegram callback UX."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from open_shrimp.config import Config, ContextConfig
from open_shrimp.db import ChatScope
from open_shrimp.dispatch_registry import dispatch
from open_shrimp.stream import StreamResult

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "suggest:"
_MAX_STORED = 256
_TTL_SECONDS = 15 * 60
_BUTTON_MAX_CHARS = 64
_suggestions: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_scope_tokens: dict[ChatScope, int] = {}
_scope_tasks: dict[ChatScope, asyncio.Task[None]] = {}

SUGGESTION_PROMPT = """[SUGGESTION MODE: Suggest what the user might naturally type next into OpenShrimp.]

FIRST: Look at the user's recent messages and original request.

Your job is to predict what THEY would type - not what you think they should do.

THE TEST: Would they think "I was just about to type that"?

EXAMPLES:
User asked "fix the bug and run tests", bug is fixed -> "run the tests"
After code written -> "try it out"
The agent offers options -> suggest the one the user would likely pick, based on conversation
The agent asks to continue -> "yes" or "go ahead"
Task complete, obvious follow-up -> "commit this" or "push it"
After error or misunderstanding -> silence (let them assess/correct)

Be specific: "run the tests" beats "continue".

NEVER SUGGEST:
- Evaluative ("looks good", "thanks")
- Questions ("what about...?")
- Assistant-voice ("Let me...", "I'll...", "Here's...")
- New ideas they didn't ask about
- Multiple sentences

Stay silent if the next step isn't obvious from what the user said.

Format: 2-12 words, match the user's style. Or nothing.

Reply with ONLY the suggestion, no quotes or explanation.
"""

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}
_ALLOWED_SINGLE_WORDS = {
    "yes", "yeah", "yep", "yea", "yup", "sure", "ok", "okay", "push",
    "commit", "deploy", "stop", "continue", "check", "exit", "quit", "no",
}


def prompt_suggestions_enabled(config: Config) -> bool:
    raw = os.getenv("OPENSHRIMP_ENABLE_PROMPT_SUGGESTION")
    if raw is not None:
        value = raw.strip().lower()
        if value in _TRUTHY:
            return True
        if value in _FALSY:
            return False
    return config.prompt_suggestions.enabled


def supersede_prompt_suggestion(scope: ChatScope) -> int:
    task = _scope_tasks.pop(scope, None)
    if task is not None and not task.done():
        task.cancel()
    token = _scope_tokens.get(scope, 0) + 1
    _scope_tokens[scope] = token
    return token


def _current_token(scope: ChatScope) -> int:
    return _scope_tokens.get(scope, 0)


def store_suggestion(text: str) -> str:
    _evict_expired()
    key = secrets.token_urlsafe(8)
    _suggestions[key] = (text, time.monotonic() + _TTL_SECONDS)
    _suggestions.move_to_end(key)
    while len(_suggestions) > _MAX_STORED:
        _suggestions.popitem(last=False)
    return key


def pop_suggestion(key: str) -> str | None:
    _evict_expired()
    item = _suggestions.pop(key, None)
    if item is None:
        return None
    return item[0]


def _evict_expired() -> None:
    now = time.monotonic()
    for key, (_, expires_at) in list(_suggestions.items()):
        if expires_at > now:
            break
        _suggestions.pop(key, None)


def filter_suggestion(text: str | None) -> tuple[bool, str]:
    if text is None:
        return False, "empty"
    candidate = text.strip().strip('"\'')
    lowered = candidate.lower()
    if not candidate:
        return False, "empty"
    if lowered == "done":
        return False, "done"
    if any(p in lowered for p in ("nothing found", "nothing to suggest", "no suggestion", "silence")):
        return False, "meta_text"
    if re.fullmatch(r"[\[(].*[\])]", candidate):
        return False, "bracketed_meta"
    if lowered.startswith(("error", "failed", "exception", "traceback")):
        return False, "error_text"
    if re.match(r"^[A-Za-z][\w -]{0,24}:\s+", candidate):
        return False, "label_prefix"
    if "\n" in candidate or "`" in candidate or "*" in candidate or "#" in candidate:
        return False, "markdown"
    if len(candidate) >= 100:
        return False, "too_long"
    sentence_marks = len(re.findall(r"[.!?](?:\s|$)", candidate))
    if sentence_marks > 1 or candidate.endswith("?"):
        return False, "multiple_sentences"
    words = re.findall(r"[\w']+", candidate)
    if len(words) < 2 and lowered not in _ALLOWED_SINGLE_WORDS:
        return False, "too_few_words"
    if len(words) > 12:
        return False, "too_many_words"
    if any(p in lowered for p in ("thanks", "thank you", "looks good", "perfect", "makes sense")):
        return False, "evaluative"
    if lowered.startswith(("let me", "i'll", "ill ", "here's", "heres", "you should", "sure,")):
        return False, "assistant_voice"
    return True, "ok"


async def should_generate(
    *,
    config: Config,
    result: StreamResult,
    pending_permission: bool,
    elicitation_active: bool,
    assistant_turn_count: int | None = None,
) -> tuple[bool, str]:
    if not prompt_suggestions_enabled(config):
        return False, "disabled"
    if not result.sent_message_ids:
        return False, "no_final_message"
    if result.last_turn_had_error:
        return False, "api_error"
    count = assistant_turn_count if assistant_turn_count is not None else result.assistant_turn_count
    if count is None:
        return False, "assistant_turn_count_unavailable"
    if count < 2:
        return False, "too_few_assistant_turns"
    usage = result.turn_usage
    if usage is None:
        return False, "usage_unavailable"
    if _usage_total(usage) > 10000:
        return False, "cache_cold_large_parent"
    if pending_permission:
        return False, "pending_permission"
    if elicitation_active:
        return False, "elicitation_active"
    # TODO: Suppress for OpenCode rate-limit state when OpenCode exposes it.
    # TODO: Suppress for OpenCode plan mode when a reliable session flag exists.
    return True, "ok"


def _usage_total(usage: dict[str, Any]) -> int:
    cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
    values = [usage.get("input"), cache.get("write"), usage.get("output")]
    total = 0
    for value in values:
        if isinstance(value, (int, float)):
            total += int(value)
    return total


async def attach_suggestion_button(
    bot: Bot,
    scope: ChatScope,
    message_id: int,
    suggestion: str,
) -> None:
    key = store_suggestion(suggestion)
    label = suggestion if len(suggestion) <= _BUTTON_MAX_CHARS else suggestion[:61].rstrip() + "..."
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}{key}")]])
    try:
        await bot.edit_message_reply_markup(
            chat_id=scope.chat_id,
            message_id=message_id,
            reply_markup=markup,
        )
        logger.info(
            "Prompt suggestion attached: scope=%s message_id=%s label=%r",
            scope,
            message_id,
            label,
        )
    except BadRequest as exc:
        logger.info(
            "Prompt suggestion attach failed: scope=%s message_id=%s error=%s",
            scope,
            message_id,
            exc,
        )


async def handle_suggestion_callback(query: Any, data: str) -> None:
    key = data[len(CALLBACK_PREFIX):]
    suggestion = pop_suggestion(key)
    if suggestion is None:
        await query.answer("Suggestion expired.")
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    message = query.message
    if message is None:
        return
    from open_shrimp.handlers.utils import chat_scope_from_message

    scope = chat_scope_from_message(message)
    await dispatch(suggestion, scope.chat_id, scope.thread_id)


def schedule_prompt_suggestion(
    *,
    bot: Bot,
    scope: ChatScope,
    config: Config,
    client: Any,
    result: StreamResult,
    context_name: str,
    context_config: ContextConfig,
) -> None:
    token = _current_token(scope)
    if token == 0:
        token = supersede_prompt_suggestion(scope)
    logger.info(
        "Prompt suggestion scheduled: scope=%s context=%s token=%s messages=%s usage=%s error=%s",
        scope,
        context_name,
        token,
        result.sent_message_ids,
        result.turn_usage,
        result.last_turn_had_error,
    )
    task = asyncio.create_task(
        _run_prompt_suggestion(
            bot=bot,
            scope=scope,
            token=token,
            config=config,
            client=client,
            result=result,
            context_name=context_name,
            context_config=context_config,
        )
    )
    _scope_tasks[scope] = task


async def _run_prompt_suggestion(
    *,
    bot: Bot,
    scope: ChatScope,
    token: int,
    config: Config,
    client: Any,
    result: StreamResult,
    context_name: str,
    context_config: ContextConfig,
) -> None:
    try:
        session_id = getattr(client, "session_id", None)
        if not session_id:
            logger.info("Prompt suggestion suppressed: scope=%s reason=session_unavailable", scope)
            return
        assistant_turn_count = await client.count_assistant_turns(session_id)
        if assistant_turn_count is None:
            logger.info(
                "Prompt suggestion suppressed: scope=%s reason=assistant_turn_count_unavailable session=%s",
                scope,
                session_id,
            )
            return
        from open_shrimp.handlers.state import _approval_futures, _question_states

        elicitation_active = any(state.scope == scope for state in _question_states.values())
        ok, reason = await should_generate(
            config=config,
            result=result,
            pending_permission=bool(_approval_futures),
            elicitation_active=elicitation_active,
            assistant_turn_count=assistant_turn_count,
        )
        if not ok:
            logger.info(
                "Prompt suggestion suppressed: scope=%s context=%s reason=%s assistant_turns=%s messages=%s usage=%s",
                scope,
                context_name,
                reason,
                assistant_turn_count,
                result.sent_message_ids,
                result.turn_usage,
            )
            return
        if token != _current_token(scope):
            logger.info(
                "Prompt suggestion suppressed: scope=%s reason=aborted token=%s current=%s",
                scope,
                token,
                _current_token(scope),
            )
            return
        logger.info(
            "Prompt suggestion generating: scope=%s context=%s session=%s assistant_turns=%s target_message=%s",
            scope,
            context_name,
            session_id,
            assistant_turn_count,
            result.sent_message_ids[-1],
        )
        raw = await client.generate_prompt_suggestion(prompt=SUGGESTION_PROMPT, timeout=30.0)
        ok, reason = filter_suggestion(raw)
        if not ok:
            logger.info(
                "Prompt suggestion filtered: scope=%s reason=%s raw=%r",
                scope,
                reason,
                raw,
            )
            return
        if token != _current_token(scope):
            logger.info(
                "Prompt suggestion suppressed: scope=%s reason=aborted_after_generation token=%s current=%s raw=%r",
                scope,
                token,
                _current_token(scope),
                raw,
            )
            return
        logger.info("Prompt suggestion accepted: scope=%s raw=%r", scope, raw)
        await attach_suggestion_button(bot, scope, result.sent_message_ids[-1], raw.strip())
    except asyncio.CancelledError:
        logger.info("Prompt suggestion task cancelled: scope=%s", scope)
    except Exception:
        logger.exception(
            "Prompt suggestion failed for %s in context %s (%s)",
            scope,
            context_name,
            context_config.directory,
        )
    finally:
        if _scope_tasks.get(scope) is asyncio.current_task():
            _scope_tasks.pop(scope, None)
