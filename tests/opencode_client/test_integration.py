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
        query_timeout=120.0,
    )
    async with OpenCodeClient(opts) as client:
        await client.query("Say hello in exactly five words.")
        text_chunks: list[str] = []
        saw_result = False
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                saw_result = True

    full = "".join(text_chunks).strip()
    assert full, "expected non-empty assistant text"
    assert saw_result, "expected a final ResultMessage"
