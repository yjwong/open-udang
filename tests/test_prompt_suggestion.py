from __future__ import annotations

import pytest

from open_shrimp.config import Config, ContextConfig, PromptSuggestionsConfig, TelegramConfig
from open_shrimp.db import ChatScope
from open_shrimp.prompt_suggestion import (
    filter_suggestion,
    pop_suggestion,
    prompt_suggestions_enabled,
    should_generate,
    store_suggestion,
)
from open_shrimp.stream import StreamResult


def _config(enabled: bool = True) -> Config:
    return Config(
        telegram=TelegramConfig(token="token"),
        allowed_users=[1],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="Default",
                allowed_tools=[],
            )
        },
        default_context="default",
        prompt_suggestions=PromptSuggestionsConfig(enabled=enabled),
    )


def test_store_pop_is_single_use() -> None:
    key = store_suggestion("run tests")
    assert pop_suggestion(key) == "run tests"
    assert pop_suggestion(key) is None


@pytest.mark.parametrize(
    ("text", "allowed"),
    [
        ("run the tests", True),
        ("yes", True),
        ("done", False),
        ("nothing to suggest", False),
        ("(silence)", False),
        ("Error: failed", False),
        ("Suggestion: run tests", False),
        ("ok", True),
        ("x", False),
        ("one two three four five six seven eight nine ten eleven twelve thirteen", False),
        ("first sentence. second sentence.", False),
        ("run\ntests", False),
        ("thanks", False),
        ("I'll run tests", False),
    ],
)
def test_filter_suggestion_cases(text: str, allowed: bool) -> None:
    assert filter_suggestion(text)[0] is allowed


def test_env_gate_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSHRIMP_ENABLE_PROMPT_SUGGESTION", "0")
    assert prompt_suggestions_enabled(_config(enabled=True)) is False
    monkeypatch.setenv("OPENSHRIMP_ENABLE_PROMPT_SUGGESTION", "1")
    assert prompt_suggestions_enabled(_config(enabled=False)) is True


@pytest.mark.asyncio
async def test_should_generate_gates() -> None:
    result = StreamResult(
        sent_message_ids=[123],
        assistant_turn_count=2,
        turn_usage={"input": 100, "output": 50, "cache": {"write": 0}},
    )
    ok, reason = await should_generate(
        config=_config(),
        result=result,
        pending_permission=False,
        elicitation_active=False,
    )
    assert (ok, reason) == (True, "ok")

    result.last_turn_had_error = True
    ok, reason = await should_generate(
        config=_config(),
        result=result,
        pending_permission=False,
        elicitation_active=False,
    )
    assert (ok, reason) == (False, "api_error")

    result.last_turn_had_error = False
    result.turn_usage = None
    ok, reason = await should_generate(
        config=_config(),
        result=result,
        pending_permission=False,
        elicitation_active=False,
    )
    assert (ok, reason) == (False, "usage_unavailable")
