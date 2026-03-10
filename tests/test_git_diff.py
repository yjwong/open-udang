"""Tests for git diff parsing and hunk extraction."""

import os
import textwrap

import pytest

from open_udang.review.git_diff import (
    Hunk,
    HunkLine,
    HunkResult,
    detect_language,
    generate_hunk_id,
    get_hunks,
    parse_diff,
)


# ---- Unit tests: language detection ----


class TestDetectLanguage:
    def test_python(self) -> None:
        assert detect_language("src/main.py") == "python"

    def test_typescript(self) -> None:
        assert detect_language("components/App.tsx") == "typescript"

    def test_javascript(self) -> None:
        assert detect_language("index.js") == "javascript"

    def test_go(self) -> None:
        assert detect_language("cmd/server/main.go") == "go"

    def test_rust(self) -> None:
        assert detect_language("src/lib.rs") == "rust"

    def test_yaml(self) -> None:
        assert detect_language("config.yaml") == "yaml"
        assert detect_language("config.yml") == "yaml"

    def test_json(self) -> None:
        assert detect_language("package.json") == "json"

    def test_markdown(self) -> None:
        assert detect_language("README.md") == "markdown"

    def test_dockerfile(self) -> None:
        assert detect_language("Dockerfile") == "dockerfile"
        assert detect_language("path/to/Dockerfile") == "dockerfile"

    def test_makefile(self) -> None:
        assert detect_language("Makefile") == "makefile"

    def test_unknown_extension(self) -> None:
        assert detect_language("file.xyz") == "text"

    def test_no_extension(self) -> None:
        assert detect_language("somefile") == "text"


# ---- Unit tests: hunk ID generation ----


class TestGenerateHunkId:
    def test_deterministic(self) -> None:
        lines = [HunkLine(type="add", old_no=None, new_no=1, content="hello")]
        id1 = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines)
        id2 = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines)
        assert id1 == id2

    def test_different_content(self) -> None:
        lines_a = [HunkLine(type="add", old_no=None, new_no=1, content="hello")]
        lines_b = [HunkLine(type="add", old_no=None, new_no=1, content="world")]
        id_a = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines_a)
        id_b = generate_hunk_id("file.py", "@@ -0,0 +1 @@", lines_b)
        assert id_a != id_b

    def test_different_file(self) -> None:
        lines = [HunkLine(type="add", old_no=None, new_no=1, content="hello")]
        id_a = generate_hunk_id("a.py", "@@ -0,0 +1 @@", lines)
        id_b = generate_hunk_id("b.py", "@@ -0,0 +1 @@", lines)
        assert id_a != id_b

    def test_length(self) -> None:
        lines = [HunkLine(type="add", old_no=None, new_no=1, content="x")]
        hunk_id = generate_hunk_id("f.py", "@@", lines)
        assert len(hunk_id) == 16


# ---- Unit tests: diff parsing ----


SIMPLE_DIFF = textwrap.dedent("""\
    diff --git a/src/main.py b/src/main.py
    index abc1234..def5678 100644
    --- a/src/main.py
    +++ b/src/main.py
    @@ -10,6 +10,8 @@ import os
     import os
     import sys
    +import json
    +import yaml

     def main():
""")


class TestParseDiff:
    def test_simple_modification(self) -> None:
        hunks = parse_diff(SIMPLE_DIFF, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.file_path == "src/main.py"
        assert hunk.language == "python"
        assert hunk.is_new_file is False
        assert hunk.is_deleted_file is False
        assert hunk.staged is False
        assert hunk.is_binary is False
        assert hunk.hunk_header == "@@ -10,6 +10,8 @@ import os"

        # Check lines.
        add_lines = [l for l in hunk.lines if l.type == "add"]
        assert len(add_lines) == 2
        assert add_lines[0].content == "import json"
        assert add_lines[1].content == "import yaml"

        context_lines = [l for l in hunk.lines if l.type == "context"]
        assert len(context_lines) >= 2

    def test_new_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/new_file.py b/new_file.py
            new file mode 100644
            index 0000000..abc1234
            --- /dev/null
            +++ b/new_file.py
            @@ -0,0 +1,3 @@
            +#!/usr/bin/env python
            +
            +print("hello")
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.is_new_file is True
        assert hunk.file_path == "new_file.py"
        assert len(hunk.lines) == 3
        assert all(l.type == "add" for l in hunk.lines)

    def test_deleted_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/old_file.py b/old_file.py
            deleted file mode 100644
            index abc1234..0000000
            --- a/old_file.py
            +++ /dev/null
            @@ -1,2 +0,0 @@
            -import os
            -print("bye")
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.is_deleted_file is True
        assert hunk.file_path == "old_file.py"
        assert all(l.type == "delete" for l in hunk.lines)

    def test_binary_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/image.png b/image.png
            index abc1234..def5678 100644
            Binary files a/image.png and b/image.png differ
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert hunk.is_binary is True
        assert hunk.file_path == "image.png"
        assert hunk.lines == []
        assert hunk.hunk_header == "(binary)"

    def test_multiple_hunks_same_file(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/big.py b/big.py
            index abc1234..def5678 100644
            --- a/big.py
            +++ b/big.py
            @@ -1,3 +1,4 @@
             line1
            +added_top
             line2
             line3
            @@ -50,3 +51,4 @@
             line50
            +added_bottom
             line51
             line52
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 2
        assert hunks[0].hunk_header.startswith("@@ -1,3")
        assert hunks[1].hunk_header.startswith("@@ -50,3")

    def test_multiple_files(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/a.py b/a.py
            index abc..def 100644
            --- a/a.py
            +++ b/a.py
            @@ -1,2 +1,3 @@
             line1
            +new_in_a
             line2
            diff --git a/b.js b/b.js
            index abc..def 100644
            --- a/b.js
            +++ b/b.js
            @@ -1,2 +1,3 @@
             const x = 1;
            +const y = 2;
             console.log(x);
        """)
        hunks = parse_diff(diff, staged=True)
        assert len(hunks) == 2
        assert hunks[0].file_path == "a.py"
        assert hunks[0].language == "python"
        assert hunks[0].staged is True
        assert hunks[1].file_path == "b.js"
        assert hunks[1].language == "javascript"
        assert hunks[1].staged is True

    def test_empty_diff(self) -> None:
        hunks = parse_diff("", staged=False)
        assert hunks == []

    def test_whitespace_only_diff(self) -> None:
        hunks = parse_diff("  \n\n  ", staged=False)
        assert hunks == []

    def test_staged_flag(self) -> None:
        hunks = parse_diff(SIMPLE_DIFF, staged=True)
        assert len(hunks) == 1
        assert hunks[0].staged is True

    def test_line_numbers(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/f.py b/f.py
            index abc..def 100644
            --- a/f.py
            +++ b/f.py
            @@ -5,4 +5,5 @@
             context_line
            -deleted_line
            +added_line1
            +added_line2
             another_context
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        lines = hunks[0].lines
        # context_line: old=5, new=5
        assert lines[0].type == "context"
        assert lines[0].old_no == 5
        assert lines[0].new_no == 5
        # deleted_line: old=6, new=None
        assert lines[1].type == "delete"
        assert lines[1].old_no == 6
        assert lines[1].new_no is None
        # added_line1: old=None, new=6
        assert lines[2].type == "add"
        assert lines[2].old_no is None
        assert lines[2].new_no == 6
        # added_line2: old=None, new=7
        assert lines[3].type == "add"
        assert lines[3].old_no is None
        assert lines[3].new_no == 7
        # another_context: old=7, new=8
        assert lines[4].type == "context"
        assert lines[4].old_no == 7
        assert lines[4].new_no == 8

    def test_rename(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/old_name.py b/new_name.py
            similarity index 90%
            rename from old_name.py
            rename to new_name.py
            index abc..def 100644
            --- a/old_name.py
            +++ b/new_name.py
            @@ -1,3 +1,3 @@
             line1
            -old_content
            +new_content
             line3
        """)
        hunks = parse_diff(diff, staged=False)
        assert len(hunks) == 1
        # Should use the "b" (destination) path.
        assert hunks[0].file_path == "new_name.py"


# ---- Integration tests: real git repo ----


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(f"cd {repo} && git init -b main && git config user.email 'test@test.com' && git config user.name 'Test'")
    # Create initial file and commit.
    (repo / "hello.py").write_text("print('hello')\n")
    os.system(f"cd {repo} && git add . && git commit -m 'initial'")
    return str(repo)


@pytest.mark.asyncio
async def test_get_hunks_no_changes(git_repo: str) -> None:
    """No changes should return empty result."""
    result = await get_hunks(git_repo)
    assert result.total_hunks == 0
    assert result.hunks == []
    assert result.offset == 0


@pytest.mark.asyncio
async def test_get_hunks_unstaged_changes(git_repo: str) -> None:
    """Unstaged modifications should appear as hunks."""
    # Modify a tracked file.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('hello')\nprint('world')\n")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 1
    hunk = result.hunks[0]
    assert hunk.file_path == "hello.py"
    assert hunk.staged is False
    assert hunk.language == "python"
    # Should have an add line for "print('world')".
    add_lines = [l for l in hunk.lines if l.type == "add"]
    assert any("world" in l.content for l in add_lines)


@pytest.mark.asyncio
async def test_get_hunks_staged_changes(git_repo: str) -> None:
    """Staged changes should appear with staged=True."""
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('hello')\nprint('staged')\n")
    os.system(f"cd {git_repo} && git add hello.py")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 1
    hunk = result.hunks[0]
    assert hunk.staged is True


@pytest.mark.asyncio
async def test_get_hunks_mixed_staged_unstaged(git_repo: str) -> None:
    """Both staged and unstaged changes should appear."""
    # Stage a change.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('staged_change')\n")
    os.system(f"cd {git_repo} && git add hello.py")

    # Make another unstaged change.
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('staged_change')\nprint('unstaged_change')\n")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 2
    staged = [h for h in result.hunks if h.staged]
    unstaged = [h for h in result.hunks if not h.staged]
    assert len(staged) == 1
    assert len(unstaged) == 1


@pytest.mark.asyncio
async def test_get_hunks_untracked_files(git_repo: str) -> None:
    """Untracked files should appear when include_untracked=True."""
    with open(os.path.join(git_repo, "new_file.txt"), "w") as f:
        f.write("new content\n")

    result = await get_hunks(git_repo, include_untracked=True)
    assert result.total_hunks >= 1
    new_hunks = [h for h in result.hunks if h.file_path == "new_file.txt"]
    assert len(new_hunks) == 1
    assert new_hunks[0].is_new_file is True


@pytest.mark.asyncio
async def test_get_hunks_untracked_excluded(git_repo: str) -> None:
    """Untracked files should not appear when include_untracked=False."""
    with open(os.path.join(git_repo, "new_file.txt"), "w") as f:
        f.write("new content\n")

    result = await get_hunks(git_repo, include_untracked=False)
    new_hunks = [h for h in result.hunks if h.file_path == "new_file.txt"]
    assert len(new_hunks) == 0


@pytest.mark.asyncio
async def test_get_hunks_pagination(git_repo: str) -> None:
    """Pagination should work correctly."""
    # Create several changes across multiple files.
    for i in range(5):
        with open(os.path.join(git_repo, f"file{i}.py"), "w") as f:
            f.write(f"content_{i}\n")

    result_all = await get_hunks(git_repo, include_untracked=True)
    total = result_all.total_hunks
    assert total >= 5

    # First page.
    result_p1 = await get_hunks(git_repo, offset=0, limit=2, include_untracked=True)
    assert len(result_p1.hunks) == 2
    assert result_p1.total_hunks == total
    assert result_p1.offset == 0

    # Second page.
    result_p2 = await get_hunks(git_repo, offset=2, limit=2, include_untracked=True)
    assert len(result_p2.hunks) == 2
    assert result_p2.offset == 2

    # Ensure no overlap.
    ids_p1 = {h.id for h in result_p1.hunks}
    ids_p2 = {h.id for h in result_p2.hunks}
    assert ids_p1.isdisjoint(ids_p2)


@pytest.mark.asyncio
async def test_get_hunks_only_staged(git_repo: str) -> None:
    """When all changes are staged, only staged hunks appear."""
    with open(os.path.join(git_repo, "hello.py"), "w") as f:
        f.write("print('updated')\n")
    os.system(f"cd {git_repo} && git add hello.py")

    result = await get_hunks(git_repo)
    assert result.total_hunks == 1
    assert result.hunks[0].staged is True
