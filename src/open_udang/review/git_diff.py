"""Git diff parsing and hunk extraction for the review app."""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# File extension to language mapping for syntax highlighting.
_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".toml": "toml",
    ".xml": "xml",
    ".lua": "lua",
    ".php": "php",
    ".swift": "swift",
    ".r": "r",
    ".R": "r",
    ".dockerfile": "dockerfile",
    ".proto": "protobuf",
}


@dataclass
class HunkLine:
    """A single line within a diff hunk."""

    type: str  # "add", "delete", or "context"
    old_no: int | None
    new_no: int | None
    content: str


@dataclass
class Hunk:
    """A parsed diff hunk with metadata."""

    id: str
    file_path: str
    language: str
    is_new_file: bool
    is_deleted_file: bool
    hunk_header: str
    lines: list[HunkLine]
    staged: bool
    is_binary: bool


@dataclass
class HunkResult:
    """Paginated result of diff hunks."""

    total_hunks: int
    offset: int
    hunks: list[Hunk]


def detect_language(file_path: str) -> str:
    """Detect programming language from file extension."""
    # Handle Dockerfile specially (no extension).
    basename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    if basename.lower() in ("dockerfile", "containerfile"):
        return "dockerfile"
    if basename.lower() == "makefile":
        return "makefile"

    dot_idx = file_path.rfind(".")
    if dot_idx == -1:
        return "text"
    ext = file_path[dot_idx:]
    return _EXT_TO_LANGUAGE.get(ext, "text")


def generate_hunk_id(file_path: str, hunk_header: str, lines: list[HunkLine]) -> str:
    """Generate a stable, deterministic hash ID for a hunk."""
    content = file_path + "\n" + hunk_header + "\n"
    for line in lines:
        content += f"{line.type}:{line.content}\n"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# Regex for the unified diff file header.
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
# Regex for the hunk header.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# Regex for binary file detection.
_BINARY_RE = re.compile(r"^Binary files .* and .* differ$")


def parse_diff(diff_text: str, staged: bool) -> list[Hunk]:
    """Parse unified diff output into structured Hunk objects.

    Args:
        diff_text: Raw output from `git diff`.
        staged: Whether this diff comes from `git diff --cached`.

    Returns:
        List of parsed Hunk objects.
    """
    hunks: list[Hunk] = []
    if not diff_text.strip():
        return hunks

    lines = diff_text.split("\n")
    i = 0

    while i < len(lines):
        # Find the next diff header.
        header_match = _DIFF_HEADER_RE.match(lines[i])
        if not header_match:
            i += 1
            continue

        file_path_a = header_match.group(1)
        file_path_b = header_match.group(2)
        # Use the "b" path (destination) as the canonical file path.
        file_path = file_path_b
        i += 1

        is_new_file = False
        is_deleted_file = False
        is_binary = False

        # Parse extended header lines (new file mode, deleted file mode, etc.).
        while i < len(lines) and not lines[i].startswith("---") and not lines[i].startswith("@@"):
            if lines[i].startswith("new file mode"):
                is_new_file = True
            elif lines[i].startswith("deleted file mode"):
                is_deleted_file = True
            elif _BINARY_RE.match(lines[i]):
                is_binary = True
            # Check for next diff header — stop processing this file.
            if _DIFF_HEADER_RE.match(lines[i]):
                break
            i += 1

        if is_binary:
            # Binary file: create a single hunk with no lines.
            hunk_header = "(binary)"
            hunk_lines: list[HunkLine] = []
            hunk_id = generate_hunk_id(file_path, hunk_header, hunk_lines)
            hunks.append(Hunk(
                id=hunk_id,
                file_path=file_path,
                language=detect_language(file_path),
                is_new_file=is_new_file,
                is_deleted_file=is_deleted_file,
                hunk_header=hunk_header,
                lines=hunk_lines,
                staged=staged,
                is_binary=True,
            ))
            continue

        # Skip --- and +++ lines.
        while i < len(lines) and (lines[i].startswith("---") or lines[i].startswith("+++")):
            i += 1

        # Parse hunks for this file.
        while i < len(lines):
            hunk_match = _HUNK_HEADER_RE.match(lines[i])
            if not hunk_match:
                # Could be a new diff header or end of input.
                break

            hunk_header = lines[i]
            old_start = int(hunk_match.group(1))
            new_start = int(hunk_match.group(3))
            i += 1

            old_no = old_start
            new_no = new_start
            hunk_lines = []

            while i < len(lines):
                line = lines[i]
                # Stop at next hunk header or diff header.
                if _HUNK_HEADER_RE.match(line) or _DIFF_HEADER_RE.match(line):
                    break
                if line.startswith("+"):
                    hunk_lines.append(HunkLine(
                        type="add",
                        old_no=None,
                        new_no=new_no,
                        content=line[1:],
                    ))
                    new_no += 1
                elif line.startswith("-"):
                    hunk_lines.append(HunkLine(
                        type="delete",
                        old_no=old_no,
                        new_no=None,
                        content=line[1:],
                    ))
                    old_no += 1
                elif line.startswith(" "):
                    hunk_lines.append(HunkLine(
                        type="context",
                        old_no=old_no,
                        new_no=new_no,
                        content=line[1:],
                    ))
                    old_no += 1
                    new_no += 1
                elif line == "\\ No newline at end of file":
                    # Git marker, skip it.
                    pass
                else:
                    # Unknown line (e.g., empty line at end of diff).
                    # An empty line could be a context line with the trailing
                    # space stripped by git.
                    if line == "":
                        # Check if we're at the end of the diff output.
                        # Peek ahead: if next line is a hunk/diff header or
                        # EOF, this is the end of the hunk.
                        if i + 1 >= len(lines) or _HUNK_HEADER_RE.match(lines[i + 1]) or _DIFF_HEADER_RE.match(lines[i + 1]):
                            i += 1
                            break
                        # Otherwise, treat as context line with empty content.
                        hunk_lines.append(HunkLine(
                            type="context",
                            old_no=old_no,
                            new_no=new_no,
                            content="",
                        ))
                        old_no += 1
                        new_no += 1
                i += 1

            if hunk_lines:
                hunk_id = generate_hunk_id(file_path, hunk_header, hunk_lines)
                hunks.append(Hunk(
                    id=hunk_id,
                    file_path=file_path,
                    language=detect_language(file_path),
                    is_new_file=is_new_file,
                    is_deleted_file=is_deleted_file,
                    hunk_header=hunk_header,
                    lines=hunk_lines,
                    staged=staged,
                    is_binary=False,
                ))

    return hunks


async def _run_git(cwd: str, *args: str) -> tuple[str, str, int]:
    """Run a git command as an async subprocess.

    Returns:
        Tuple of (stdout, stderr, returncode).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode


async def _get_untracked_files(cwd: str) -> list[str]:
    """Get list of untracked files in the working directory."""
    stdout, _, rc = await _run_git(cwd, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        return []
    return [f for f in stdout.strip().split("\n") if f]


async def _add_intent_to_add(cwd: str, files: list[str]) -> None:
    """Mark untracked files with --intent-to-add so they appear in diffs."""
    if not files:
        return
    _, stderr, rc = await _run_git(cwd, "add", "--intent-to-add", "--", *files)
    if rc != 0:
        logger.warning("git add --intent-to-add failed: %s", stderr.strip())


async def get_hunks(
    cwd: str,
    offset: int = 0,
    limit: int = 20,
    include_untracked: bool = True,
) -> HunkResult:
    """Get paginated diff hunks from a git working directory.

    Combines both staged and unstaged changes. Optionally includes
    untracked files via --intent-to-add.

    Args:
        cwd: Working directory (must be inside a git repo).
        offset: Number of hunks to skip.
        limit: Maximum number of hunks to return.
        include_untracked: Whether to include untracked files.

    Returns:
        HunkResult with total count and paginated hunk list.
    """
    # Handle untracked files first.
    if include_untracked:
        untracked = await _get_untracked_files(cwd)
        if untracked:
            await _add_intent_to_add(cwd, untracked)

    # Run both diffs concurrently.
    unstaged_task = _run_git(cwd, "diff", "--no-color", "-U3")
    staged_task = _run_git(cwd, "diff", "--cached", "--no-color", "-U3")
    (unstaged_out, unstaged_err, unstaged_rc), (staged_out, staged_err, staged_rc) = (
        await asyncio.gather(unstaged_task, staged_task)
    )

    if unstaged_rc != 0:
        logger.warning("git diff failed: %s", unstaged_err.strip())
    if staged_rc != 0:
        logger.warning("git diff --cached failed: %s", staged_err.strip())

    # Parse both diffs.
    unstaged_hunks = parse_diff(unstaged_out, staged=False)
    staged_hunks = parse_diff(staged_out, staged=True)

    # Combine: staged hunks first, then unstaged.
    all_hunks = staged_hunks + unstaged_hunks
    total = len(all_hunks)

    # Apply pagination.
    paginated = all_hunks[offset:offset + limit]

    return HunkResult(
        total_hunks=total,
        offset=offset,
        hunks=paginated,
    )
