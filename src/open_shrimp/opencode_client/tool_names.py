"""Tool-name and permission-category translation tables.

OpenCode and open-shrimp's hooks.py use different vocabularies for tool
names. We carry two related-but-distinct maps:

* ``OPENCODE_TO_HOOKS`` / ``HOOKS_TO_OPENCODE`` — *tool names*. Translates
  the wire name on ``ToolPart.tool`` (lowercase: ``bash``, ``read``,
  ``edit``, …) to the literal-string identifiers ``hooks.py`` checks
  (``"Bash"``, ``"Read"``, ``"Edit"``, …). Used when synthesising
  ``ToolUseBlock.name`` from a ``message.part.updated`` event.

* ``CATEGORY_TO_HOOKS`` — *permission categories*. The ``permission``
  field on a ``permission.asked`` event is a category, not a tool name
  (see ``permission.ts`` in opencode-core). ``edit`` covers
  ``{edit, write, apply_patch}``; ``external_directory`` is path-scoped
  and depends on the active tool. Used by the permission bridge.
"""

from __future__ import annotations


# Tool-name table — wire name → hooks vocabulary.
OPENCODE_TO_HOOKS: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "glob": "Glob",
    "grep": "Grep",
    "list": "LS",
    "webfetch": "WebFetch",
    "webwrite": "WebWrite",
    "todowrite": "TodoWrite",
    "apply_patch": "ApplyPatch",
    "task": "Task",
}

HOOKS_TO_OPENCODE: dict[str, str] = {v: k for k, v in OPENCODE_TO_HOOKS.items()}


def opencode_to_hooks(name: str) -> str:
    """Translate an OpenCode tool name to the hooks vocabulary.

    Unknown names — including MCP tool names like ``openshrimp_send_file``
    — pass through unchanged.
    """
    return OPENCODE_TO_HOOKS.get(name, name)


def hooks_to_opencode(name: str) -> str:
    """Translate a hooks tool name to the OpenCode wire name."""
    return HOOKS_TO_OPENCODE.get(name, name)


# Permission-category table — the ``permission`` field on
# ``permission.asked`` → hooks tool name. ``edit`` and
# ``external_directory`` need the in-flight ToolPart to disambiguate; the
# bridge handles that and only consults this map as a fallback.
CATEGORY_TO_HOOKS: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "edit": "Edit",  # disambiguated to Edit/Write/ApplyPatch by ToolPart.tool
    "webfetch": "WebFetch",
    "webwrite": "WebWrite",
    "todowrite": "TodoWrite",
}


# Categories the OpenCode baseline defaults to ``allow`` (see
# ``opencode/packages/core/src/agent/agent.ts``). We rewrite these to
# ``ask`` at session-create so open-shrimp's hooks.py owns the policy.
# ``question`` is intentionally not mapped to ``AskUserQuestion``; its
# blocking lifecycle is handled by the native ``question.asked`` event arm.
OPENCODE_PERMISSION_CATEGORIES: tuple[str, ...] = (
    "bash",
    "read",
    "edit",
    "question",
    "webfetch",
    "webwrite",
    "external_directory",
)
