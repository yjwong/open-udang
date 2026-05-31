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


def test_openshrimp_schedule_mutations_ask_by_default() -> None:
    rules = _client(["openshrimp_send_file"])._build_initial_rules()

    assert {
        "permission": "openshrimp_create_schedule",
        "pattern": "*",
        "action": "ask",
    } in rules
    assert {
        "permission": "openshrimp_delete_schedule",
        "pattern": "*",
        "action": "ask",
    } in rules


def test_openshrimp_schedule_mutations_can_be_allowed_explicitly() -> None:
    rules = _client(["openshrimp_create_schedule"])._build_initial_rules()

    matching = [
        rule for rule in rules
        if rule["permission"] == "openshrimp_create_schedule"
    ]
    assert matching[-1] == {
        "permission": "openshrimp_create_schedule",
        "pattern": "*",
        "action": "allow",
    }


def test_old_sdk_mcp_tool_names_map_to_opencode_names() -> None:
    rules = _client(["mcp__openshrimp__send_file"])._build_initial_rules()

    assert {
        "permission": "openshrimp_send_file",
        "pattern": "*",
        "action": "allow",
    } in rules


def test_host_bash_is_never_auto_allowed_from_config() -> None:
    rules = _client([
        "openshrimp_host_bash",
        "mcp__openshrimp__host_bash",
    ])._build_initial_rules()

    assert not any(
        rule["permission"] == "openshrimp_host_bash"
        and rule["action"] == "allow"
        for rule in rules
    )


def test_builtin_task_tool_is_disabled_by_default() -> None:
    rules = _client()._build_initial_rules()

    assert rules[-1] == {
        "permission": "task",
        "pattern": "*",
        "action": "deny",
    }


def test_builtin_task_tool_deny_overrides_allowed_tools() -> None:
    rules = _client(["Task", "task"])._build_initial_rules()

    matching = [rule for rule in rules if rule["permission"] == "task"]
    assert matching[-1] == {
        "permission": "task",
        "pattern": "*",
        "action": "deny",
    }
