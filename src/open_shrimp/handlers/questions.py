"""Native OpenCode question handling via Telegram inline keyboards."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.config import Config
from open_shrimp.db import ChatScope
from open_shrimp.handlers.state import (
    _QuestionState,
    _pending_other_input,
    _question_states,
)
from open_shrimp.handlers.utils import _escape_mdv2, _is_authorized
from open_shrimp.stream import _DraftState, finalize_and_reset

logger = logging.getLogger(__name__)


def _build_question_keyboard(state: _QuestionState) -> InlineKeyboardMarkup:
    """Build inline keyboard for a question's options."""
    qid = state.question_id
    buttons: list[list[InlineKeyboardButton]] = []

    for i, opt in enumerate(state.options):
        label = opt.get("label", f"Option {i + 1}")
        if state.multi_select:
            prefix = "\u2713 " if i in state.selected else ""
            cb_data = f"q_toggle:{qid}:{i}"
        else:
            prefix = ""
            cb_data = f"q_opt:{qid}:{i}"
        buttons.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=cb_data)])

    # Show any "Other" texts already entered (multi-select)
    for j, txt in enumerate(state.other_texts):
        display = txt[:30] + ("\u2026" if len(txt) > 30 else "")
        buttons.append([InlineKeyboardButton(f"\u2713 {display}", callback_data=f"q_noop:{qid}")])

    # "Other" button for custom text input
    if state.allow_custom:
        buttons.append([InlineKeyboardButton("Other\u2026", callback_data=f"q_other:{qid}")])

    if state.multi_select:
        count = len(state.selected) + len(state.other_texts)
        done_label = f"Done ({count} selected)" if count else "Done"
        buttons.append([InlineKeyboardButton(done_label, callback_data=f"q_done:{qid}")])

    return InlineKeyboardMarkup(buttons)


def _format_question_text(question: dict[str, Any]) -> str:
    """Format a question with its header and option descriptions."""
    question_text = question.get("question", "")
    header = question.get("header", "")
    options = question.get("options", [])

    parts: list[str] = []
    if header:
        parts.append(f"\u2753 *{_escape_mdv2(header)}*")
    parts.append(_escape_mdv2(question_text))

    for opt in options:
        label = opt.get("label", "")
        desc = opt.get("description", "")
        if desc:
            parts.append(f"\u2022 *{_escape_mdv2(label)}* \u2014 {_escape_mdv2(desc)}")

    return "\n".join(parts)


async def _send_question_keyboard(
    bot: Bot,
    scope: ChatScope,
    question: dict[str, Any],
) -> Any:
    """Present a question via inline keyboard and wait for the user's answer.

    Returns the selected option label (or custom "Other" text).
    """
    options = question.get("options", [])
    multi_select = question.get("multiSelect", question.get("multiple", False))
    allow_custom = question.get("custom", True)
    structured_answers = bool(question.get("_structuredAnswers", False))
    question_id = uuid.uuid4().hex[:8]

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    state = _QuestionState(
        question_id=question_id,
        scope=scope,
        options=options,
        multi_select=multi_select,
        future=future,
        structured_answers=structured_answers,
        allow_custom=bool(allow_custom),
    )
    _question_states[question_id] = state

    keyboard = _build_question_keyboard(state)
    text = _format_question_text(question)

    thread_kwargs: dict[str, Any] = {}
    if scope.thread_id is not None:
        thread_kwargs["message_thread_id"] = scope.thread_id

    msg = await bot.send_message(
        chat_id=scope.chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
        **thread_kwargs,
    )
    state.message_id = msg.message_id
    state.original_text_md = text

    try:
        return await future
    finally:
        _question_states.pop(question_id, None)


async def _handle_questions(
    bot: Bot,
    scope: ChatScope,
    questions: list[dict[str, Any]],
    draft_state: _DraftState,
) -> list[list[str]]:
    """Present native OpenCode questions and return structured answers."""
    await finalize_and_reset(bot, draft_state)

    answers: list[list[str]] = []
    for q in questions:
        q = dict(q)
        q["multiSelect"] = q.get("multiSelect", q.get("multiple", False))
        q.setdefault("custom", True)
        q["_structuredAnswers"] = True
        answer = await _send_question_keyboard(bot, scope, q)
        if isinstance(answer, list):
            answers.append([str(item) for item in answer])
        else:
            answers.append([str(answer)])

    return answers


async def _complete_other_input(
    bot: Bot,
    state: _QuestionState,
    custom_text: str,
) -> None:
    """Complete the 'Other...' flow after the user has typed their answer.

    For single-select questions, resolves the future immediately.
    For multi-select, adds the text to other_texts and updates the keyboard
    so the user can continue selecting or press Done.
    """
    query = state.other_query
    state.other_query = None

    original_md = state.original_text_md

    if state.multi_select:
        # Add to other_texts and restore keyboard; user still needs to press Done
        state.other_texts.append(custom_text)
        keyboard = _build_question_keyboard(state)
        if query and query.message:
            try:
                await query.message.edit_text(
                    text=original_md,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to restore question keyboard after Other")
    else:
        # Single-select: resolve with custom text
        state.future.set_result(custom_text)
        if query and query.message:
            try:
                await query.message.edit_text(
                    text=original_md + f"\n\n\u2705 *Answer:* {_escape_mdv2(custom_text)}",
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message after Other")


async def _handle_question_callback(
    query: Any, data: str, config: Config
) -> bool:
    """Handle question-related callback queries. Returns True if handled."""
    if not data.startswith("q_"):
        return False

    if not _is_authorized(query.from_user and query.from_user.id, config):
        await query.answer("Unauthorized.")
        return True

    # Parse callback data
    parts = data.split(":", 2)
    action = parts[0]  # q_opt, q_toggle, q_done, q_other, q_noop

    if action == "q_noop":
        await query.answer()
        return True

    if len(parts) < 2:
        await query.answer("Invalid callback data.")
        return True

    question_id = parts[1]
    state = _question_states.get(question_id)
    if not state or state.future.done():
        await query.answer("This question has expired.")
        return True

    if state.waiting_for_other:
        await query.answer("Please type your answer first.")
        return True

    if action == "q_opt":
        # Single-select: resolve immediately with the selected option label
        option_idx = int(parts[2]) if len(parts) > 2 else 0
        if 0 <= option_idx < len(state.options):
            label = state.options[option_idx].get("label", f"Option {option_idx + 1}")
            state.future.set_result(label)
            await query.answer(f"Selected: {label}")

            # Update message to show selection, remove keyboard
            if query.message:
                try:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    await query.message.edit_text(
                        text=original_md + f"\n\n\u2705 *Selected:* {_escape_mdv2(label)}",
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
                except Exception:
                    logger.exception("Failed to update question message")
        return True

    if action == "q_toggle":
        # Multi-select: toggle option
        option_idx = int(parts[2]) if len(parts) > 2 else 0
        if 0 <= option_idx < len(state.options):
            if option_idx in state.selected:
                state.selected.discard(option_idx)
            else:
                state.selected.add(option_idx)

            # Update keyboard to reflect toggled state
            keyboard = _build_question_keyboard(state)
            await query.answer()
            if query.message:
                try:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    await query.message.edit_reply_markup(reply_markup=keyboard)
                except Exception:
                    logger.exception("Failed to update question keyboard")
        return True

    if action == "q_done":
        # Multi-select: finalize with all selected options
        labels: list[str] = []
        for idx in sorted(state.selected):
            if 0 <= idx < len(state.options):
                labels.append(state.options[idx].get("label", f"Option {idx + 1}"))
        labels.extend(state.other_texts)

        result = ", ".join(labels) if labels else "None selected"
        if state.structured_answers:
            state.future.set_result(labels)
        else:
            state.future.set_result(result)
        await query.answer(f"Done: {result[:50]}")

        # Update message to show selections, remove keyboard
        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                await query.message.edit_text(
                    text=original_md + f"\n\n\u2705 *Selected:* {_escape_mdv2(result)}",
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message")
        return True

    if action == "q_other":
        if not state.allow_custom:
            await query.answer("Custom answers are not available.")
            return True
        # "Other..." -- mark that we're waiting for a typed answer.
        # We must NOT await here because python-telegram-bot processes
        # updates sequentially by default; blocking would deadlock the
        # message_handler that needs to deliver the typed text.
        await query.answer()

        state.waiting_for_other = True
        state.other_query = query
        _pending_other_input[state.scope] = question_id

        # Hide the keyboard and prompt the user to type their answer.
        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                await query.message.edit_text(
                    text=original_md + "\n\n\u270f\ufe0f _Type your answer below:_",
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message for Other prompt")
        return True

    return False
