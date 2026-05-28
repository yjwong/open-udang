"""Tests for tool_names translation tables."""

from __future__ import annotations

import ast
from pathlib import Path

from open_shrimp.opencode_client.tool_names import (
    CATEGORY_TO_HOOKS,
    HOOKS_TO_OPENCODE,
    OPENCODE_PERMISSION_CATEGORIES,
    OPENCODE_TO_HOOKS,
    hooks_to_opencode,
    opencode_to_hooks,
)


def test_round_trip_known_tools() -> None:
    for hooks_name, opencode_name in [
        ("Bash", "bash"),
        ("Read", "read"),
        ("Edit", "edit"),
        ("Write", "write"),
        ("Glob", "glob"),
        ("Grep", "grep"),
    ]:
        assert opencode_to_hooks(opencode_name) == hooks_name
        assert hooks_to_opencode(hooks_name) == opencode_name


def test_unknown_names_pass_through() -> None:
    # MCP-qualified names and oddities pass through untranslated.
    assert opencode_to_hooks("openshrimp_send_file") == "openshrimp_send_file"
    assert opencode_to_hooks("") == ""
    assert hooks_to_opencode("mcp__openshrimp__host_bash") == (
        "mcp__openshrimp__host_bash"
    )


def test_hooks_to_opencode_is_inverse() -> None:
    for k, v in OPENCODE_TO_HOOKS.items():
        assert HOOKS_TO_OPENCODE[v] == k


def test_category_table_subset_of_baseline() -> None:
    # Every category we map should appear in the ask-baseline list (modulo
    # tools resolved via the in-flight ToolPart).
    for cat in CATEGORY_TO_HOOKS:
        # `read` and `edit` and `bash` are in the baseline; `todowrite`
        # may not be (it's a no-prompt category in OpenCode core), but we
        # still want hooks routing for it if it ever comes through.
        assert cat in {
            "bash", "read", "edit", "webfetch", "webwrite", "todowrite",
        }


def test_baseline_covers_open_shrimp_categories() -> None:
    # Ensure the canonical set of categories the wrapper rewrites to "ask"
    # is exactly the open-shrimp-relevant subset.
    assert "bash" in OPENCODE_PERMISSION_CATEGORIES
    assert "edit" in OPENCODE_PERMISSION_CATEGORIES
    assert "read" in OPENCODE_PERMISSION_CATEGORIES
    assert "external_directory" in OPENCODE_PERMISSION_CATEGORIES


def test_hooks_literals_covered() -> None:
    """Every tool-name literal hooks.py uses must round-trip via the table.

    Static-analyses hooks.py for ``tool_name == "X"`` and ``tool_name in
    {...}``-style checks against literals, then asserts that every hooks
    literal that names a built-in OpenCode tool has a HOOKS_TO_OPENCODE
    entry. MCP / `host_bash` literals are exempt.
    """
    hooks_file = Path(__file__).resolve().parents[2] / (
        "src/open_shrimp/hooks.py"
    )
    src = hooks_file.read_text(encoding="utf-8")
    tree = ast.parse(src)

    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left_targets_tool_name = (
                isinstance(node.left, ast.Name) and node.left.id == "tool_name"
            )
            if not left_targets_tool_name:
                continue
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.In)):
                    if isinstance(comp, ast.Constant) and isinstance(
                        comp.value, str
                    ):
                        literals.add(comp.value)
                    elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                        for elt in comp.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
                                literals.add(elt.value)
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in {
                    "_MUTATING_PATH_TOOLS",
                    "_FILE_TARGETED_PATH_TOOLS",
                }:
                    if isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
                                literals.add(elt.value)

    # Strings the hooks file uses that aren't OpenCode built-in tools.
    exempt = {
        "Monitor",  # SDK-specific concept; not an OpenCode built-in tool
        "mcp__openshrimp__port_forward",
    }
    expected = {l for l in literals if l not in exempt}
    missing = expected - set(HOOKS_TO_OPENCODE)
    assert not missing, (
        f"hooks.py references built-in tools missing from "
        f"HOOKS_TO_OPENCODE: {sorted(missing)}"
    )
