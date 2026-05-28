from __future__ import annotations

from open_shrimp.opencode_client.client import OpenCodeClient
from open_shrimp.opencode_client.options import OpenCodeOptions


def _client(allowed_tools: list[str] | None = None) -> OpenCodeClient:
    return OpenCodeClient(
        OpenCodeOptions(
            cwd="/tmp/project",
            provider="openai",
            model="gpt-5",
            allowed_tools=allowed_tools,
        )
    )


def test_todowrite_is_auto_allowed_by_default() -> None:
    rules = _client()._build_initial_rules()

    assert {
        "permission": "todowrite",
        "pattern": "*",
        "action": "allow",
    } in rules


def test_mutating_tools_are_not_auto_allowed_from_config() -> None:
    rules = _client(["Edit", "Write", "ApplyPatch"])._build_initial_rules()

    assert not any(
        rule["permission"] in {"edit", "write", "apply_patch"}
        and rule["action"] == "allow"
        for rule in rules
    )
