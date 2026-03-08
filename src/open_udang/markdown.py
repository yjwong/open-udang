"""GFM to Telegram MarkdownV2 converter.

Converts GitHub-Flavored Markdown to Telegram MarkdownV2 format using mistune
to parse the AST, then walks the tree to emit Telegram-compatible markup.
"""

from __future__ import annotations

import re
from typing import Any

import mistune

TELEGRAM_MAX_LENGTH = 4096

# Characters that must be escaped in Telegram MarkdownV2 (outside code spans/blocks).
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"
_ESCAPE_RE = re.compile(r"([" + re.escape(_ESCAPE_CHARS) + r"])")


def _escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _ESCAPE_RE.sub(r"\\\1", text)


def _plain_text(token: dict[str, Any]) -> str:
    """Extract plain text from a token tree (no formatting)."""
    if "raw" in token and isinstance(token["raw"], str):
        return token["raw"]
    if "children" in token:
        children = token["children"]
        if isinstance(children, str):
            return children
        if isinstance(children, list):
            return "".join(_plain_text(c) for c in children)
    return ""


class TelegramRenderer(mistune.BaseRenderer):
    """Render mistune AST tokens into Telegram MarkdownV2 strings.

    Follows the same render_token pattern as mistune's HTMLRenderer:
    methods receive pre-rendered children text + keyword attrs.
    """

    NAME = "telegram"

    def render_token(self, token: dict[str, Any], state: Any) -> str:
        ttype = token["type"]

        # Tables need raw token access to extract cell text
        if ttype == "table":
            return self._render_table(token, state)

        func = self._get_method(ttype)
        attrs = token.get("attrs", {})

        if "raw" in token:
            children = token["raw"]
        elif "children" in token:
            children = self.render_tokens(token["children"], state)
        else:
            return func(**attrs) if attrs else func()

        return func(children, **attrs) if attrs else func(children)

    # ── Block-level ──

    def text(self, text: str) -> str:
        return _escape(text)

    def paragraph(self, text: str) -> str:
        return text + "\n\n"

    def heading(self, text: str, **attrs: Any) -> str:
        return f"*{text}*\n\n"

    def blank_line(self) -> str:
        return ""

    def thematic_break(self) -> str:
        return _escape("---") + "\n\n"

    def block_code(self, code: str, **attrs: Any) -> str:
        info = attrs.get("info", "")
        code = code.rstrip("\n")
        if info:
            return f"```{info}\n{code}\n```\n\n"
        return f"```\n{code}\n```\n\n"

    def block_quote(self, text: str) -> str:
        lines = text.strip().split("\n")
        quoted = "\n".join(">" + line for line in lines)
        return quoted + "\n\n"

    def list(self, text: str, **attrs: Any) -> str:
        return text + "\n"

    def list_item(self, text: str) -> str:
        return _escape("- ") + text.strip() + "\n"

    def block_text(self, text: str) -> str:
        return text

    def block_error(self, text: str) -> str:
        return ""

    # ── Tables → monospace preformatted ──
    # Tables need access to the raw token tree, so we override render_token
    # for the table type and handle it specially.

    def _render_table(self, token: dict[str, Any], state: Any) -> str:
        rows: list[list[str]] = []
        for child in token.get("children", []):
            if child["type"] == "table_head":
                row = [_plain_text(cell) for cell in child.get("children", [])]
                rows.append(row)
            elif child["type"] == "table_body":
                for table_row in child.get("children", []):
                    row = [_plain_text(cell) for cell in table_row.get("children", [])]
                    rows.append(row)

        if not rows:
            return ""

        col_count = max(len(r) for r in rows)
        col_widths = [0] * col_count
        for r in rows:
            for i, cell in enumerate(r):
                col_widths[i] = max(col_widths[i], len(cell))

        lines: list[str] = []
        for idx, r in enumerate(rows):
            padded = [
                (r[i] if i < len(r) else "").ljust(col_widths[i])
                for i in range(col_count)
            ]
            lines.append(" | ".join(padded))
            if idx == 0:
                lines.append("-+-".join("-" * w for w in col_widths))

        table_text = "\n".join(lines)
        return f"```\n{table_text}\n```\n\n"

    # Stubs so mistune finds the method; actual rendering is in _render_table
    def table(self, text: str) -> str:
        return text  # pragma: no cover

    def table_head(self, text: str) -> str:
        return text  # pragma: no cover

    def table_body(self, text: str) -> str:
        return text  # pragma: no cover

    def table_cell(self, text: str, **attrs: Any) -> str:
        return text  # pragma: no cover

    # ── Inline-level ──

    def emphasis(self, text: str) -> str:
        return f"_{text}_"

    def strong(self, text: str) -> str:
        return f"*{text}*"

    def codespan(self, code: str) -> str:
        return f"`{code}`"

    def link(self, text: str, **attrs: Any) -> str:
        url = attrs.get("url", "")
        # Escape only ) and \ in URLs for MarkdownV2
        escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{text}]({escaped_url})"

    def image(self, text: str, **attrs: Any) -> str:
        # Strip images, return alt text (already rendered from children)
        return text if text else ""

    def linebreak(self) -> str:
        return "\n"

    def softbreak(self) -> str:
        return "\n"

    def inline_html(self, html: str) -> str:
        return ""

    def block_html(self, html: str) -> str:
        return ""

    def strikethrough(self, text: str) -> str:
        return f"~{text}~"


def _is_inside_code_block(text: str, position: int) -> tuple[bool, str]:
    """Check if a position in rendered MarkdownV2 text is inside a code block.

    Counts unmatched ``` fences before the position.  Returns (is_inside, fence)
    where fence is the opening fence line (e.g. "```python") so we can re-open
    it in the next chunk.
    """
    inside = False
    fence = "```"
    i = 0
    while i < position:
        if text[i:i + 3] == "```":
            if not inside:
                # Capture the full fence line (e.g. ```python)
                end = text.find("\n", i)
                if end == -1 or end > position:
                    end = position
                fence = text[i:end]
                inside = True
            else:
                inside = False
            i += 3
        else:
            i += 1
    return inside, fence


def _split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split a rendered message into chunks of at most max_length characters.

    Splits at natural boundaries: paragraph breaks, then line breaks.
    Code-block-aware: if the split point falls inside a fenced code block,
    the current chunk is closed with ``` and the next chunk reopens the fence.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining.strip())
            break

        # Try to split at a paragraph boundary (double newline)
        split_at = remaining.rfind("\n\n", 0, max_length)
        if split_at <= 0:
            # Try to split at a single newline
            split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            # Last resort: split at max_length
            split_at = max_length

        chunk = remaining[:split_at].strip()
        rest = remaining[split_at:].lstrip("\n")

        # Check if we're splitting inside a code block
        inside, fence = _is_inside_code_block(remaining, split_at)
        if inside:
            # Close the code block in this chunk, reopen in the next
            chunk = chunk + "\n```"
            rest = fence + "\n" + rest

        chunks.append(chunk)
        remaining = rest

    return [c for c in chunks if c]


def gfm_to_telegram(text: str) -> list[str]:
    """Convert GitHub-Flavored Markdown to Telegram MarkdownV2.

    Returns a list of strings, each at most 4096 characters, suitable for
    sending as individual Telegram messages.
    """
    md = mistune.create_markdown(
        renderer=TelegramRenderer(),
        plugins=["strikethrough", "table"],
    )
    rendered = md(text)
    return _split_message(rendered)
