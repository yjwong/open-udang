"""Integration test against the real `opencode serve`.

Skipped unless `RUN_INTEGRATION=1`. Requires a working OpenCode install
(env: OPENCODE_BIN or ~/.opencode/bin/opencode) and a provider
configured for whatever OPENCODE_TEST_PROVIDER / OPENCODE_TEST_MODEL
point at (defaults: openai / gpt-5-mini).
"""

from __future__ import annotations

import os

import pytest

from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    OpenCodeOptions,
    ResultMessage,
    TextBlock,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 to run",
    ),
]


async def test_hello_world(tmp_path) -> None:
    provider = os.environ.get("OPENCODE_TEST_PROVIDER", "openai")
    model = os.environ.get("OPENCODE_TEST_MODEL", "gpt-5.4-mini")

    opts = OpenCodeOptions(
        cwd=str(tmp_path),
        provider=provider,
        model=model,
    )
    async with OpenCodeClient(opts) as client:
        await client.query("Say hello in exactly five words.")
        text_chunks: list[str] = []
        assistant_usages: list[dict] = []
        final_result: ResultMessage | None = None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_chunks.append(block.text)
                if msg.usage is not None:
                    assistant_usages.append(msg.usage)
            elif isinstance(msg, ResultMessage):
                final_result = msg

    full = "".join(text_chunks).strip()
    assert full, "expected non-empty assistant text"
    assert final_result is not None, "expected a final ResultMessage"

    # Phase 2.1: metadata fields populated from real wire data.
    assert final_result.num_steps >= 1, (
        f"expected at least one step, got num_steps={final_result.num_steps}"
    )
    assert final_result.model_usage, (
        f"expected non-empty model_usage, got {final_result.model_usage!r}"
    )
    assert any(model in key for key in final_result.model_usage), (
        f"expected model_usage keyed by {model!r}, got keys "
        f"{list(final_result.model_usage)!r}"
    )
    # OpenCode reports ``cost`` verbatim from the provider; some
    # accounts (OAuth/included tiers) get $0. Only assert presence.
    assert final_result.total_cost_usd is not None, (
        "expected total_cost_usd to be populated (even if 0.0)"
    )
    assert final_result.errors == [], (
        f"expected no errors on happy path, got {final_result.errors!r}"
    )
    assert assistant_usages, "expected at least one AssistantMessage.usage"
    assert any(u.get("input", 0) > 0 for u in assistant_usages), (
        f"expected non-zero input tokens, got {assistant_usages!r}"
    )
