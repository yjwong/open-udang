from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from open_shrimp.handlers.approval import _send_approval_keyboard
from open_shrimp.handlers.state import _approval_futures
from open_shrimp.stream import _SUPPRESS_NOTIFICATION_TOOLS


class _FakeBot:
    def __init__(self) -> None:
        self.reply_markup: Any = None

    async def send_message(self, **kwargs: Any) -> Any:
        self.reply_markup = kwargs.get("reply_markup")
        return SimpleNamespace(message_id=123)


@pytest.mark.asyncio
async def test_apply_patch_keyboard_omits_generic_accept_all(tmp_path) -> None:
    bot = _FakeBot()
    tool_use_id = "tu_apply_patch"
    task = asyncio.create_task(
        _send_approval_keyboard(
            bot=bot,  # type: ignore[arg-type]
            chat_id=1,
            tool_name="ApplyPatch",
            tool_input={
                "patchText": "\n".join((
                    "*** Begin Patch",
                    "*** Update File: a.py",
                    "@@",
                    "-old",
                    "+new",
                    "*** End Patch",
                )),
            },
            tool_use_id=tool_use_id,
            cwd=str(tmp_path),
        ),
    )

    try:
        while bot.reply_markup is None:
            await asyncio.sleep(0)

        labels = [
            button.text
            for row in bot.reply_markup.inline_keyboard
            for button in row
        ]
        assert "Accept all edits" in labels
        assert "Accept all ApplyPatch" not in labels

        _approval_futures[f"approve:{tool_use_id}"].set_result(False)
        assert await task is False
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for key in list(_approval_futures):
            if tool_use_id in key:
                _approval_futures.pop(key, None)


def test_apply_patch_stream_notification_is_suppressed() -> None:
    assert "ApplyPatch" in _SUPPRESS_NOTIFICATION_TOOLS
