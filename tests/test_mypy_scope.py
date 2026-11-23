"""The mypy scope is declared once, in pyproject.toml, and nothing may narrow it.

``[tool.mypy] files = ["src", "tests"]`` is the single source of truth, so the bare command
``uv run mypy`` is the only form allowed in CI, in pre-commit and in the docs. A trailing path
argument (``mypy src``) would keep CI green while a whole tree went unchecked, so this module
holds the single scope true mechanically instead of by review.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
PRECOMMIT = ROOT / ".pre-commit-config.yaml"
WORKFLOW = ROOT / ".github/workflows/release.yml"
RELEASING = ROOT / "docs/releasing.md"

#: The one scope every command relies on, in the order pyproject.toml declares it.
SCOPE = ["src", "tests"]
#: Settings that would let a file inside the scope go unchecked anyway.
SUPPRESSION_KEYS = ("disable_error_code", "ignore_errors", "ignore_missing_imports")
#: ``mypy`` and the words glued to it, stopping at punctuation such as a closing backtick.
INVOCATION = re.compile(r"\bmypy\b(?:\s+[-A-Za-z0-9_./]+)*")


def _mypy_settings() -> dict[str, object]:
    """Return the ``[tool.mypy]`` table."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        table = tomllib.load(handle)["tool"]["mypy"]
    assert isinstance(table, dict), "pyproject.toml has no [tool.mypy] table"
    return cast("dict[str, object]", table)


def _mypy_command_paths(command: str) -> list[str]:
    """Return the path arguments *command* gives mypy, ignoring its flags.

    :param command: a shell command invoking mypy, e.g. ``uv run --frozen mypy src``.
    :returns: the words after ``mypy`` that are not flags, in order.
    """
    words = command.split()
    if "mypy" not in words:
        return []
    return [word for word in words[words.index("mypy") + 1 :] if not word.startswith("-")]


def _narrowing_paths(command: str) -> list[str]:
    """Return the arguments naming something on disk, so ``mypy is strict`` is not a command.

    :param command: a shell command invoking mypy.
    :returns: the arguments naming a file or directory in the tree.
    """
    return [
        argument
        for argument in _mypy_command_paths(command)
        if argument.endswith(".py") or "/" in argument or (ROOT / argument).exists()
    ]


def _yaml_field(text: str, header: str, key: str) -> str:
    """Return the *key* value inside the YAML block that *header* opens.

    The block ends at the next line at the same indent or shallower, so a hook or a workflow
    step is read on its own.

    :param text: the whole YAML file.
    :param header: the stripped line opening the block, e.g. ``- id: mypy``.
    :param key: the field to read inside it, e.g. ``entry``.
    :returns: the value, stripped.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != header:
            continue
        indent = len(line) - len(line.lstrip())
        block = [line]
        for follower in lines[index + 1 :]:
            if follower.strip() and len(follower) - len(follower.lstrip()) <= indent:
                break
            block.append(follower)
        for field in block:
            if field.strip().startswith(f"{key}:"):
                return field.split(":", 1)[1].strip()
        raise AssertionError(f"`{header}` has no `{key}:`")
    raise AssertionError(f"no `{header}` block")


def test_pyproject_declares_the_one_scope_without_suppressions() -> None:
    """The scope is declared once, in pyproject.toml, and that declaration has no escape hatch."""
    settings = _mypy_settings()
    files = settings["files"]
    assert isinstance(files, list), "the mypy scope is declared once, in pyproject.toml"
    assert set(SCOPE) <= set(files), (
        f"[tool.mypy] files must cover {SCOPE}: narrowing it here silently stops checking a tree"
    )
    assert settings["strict"] is True, "mypy strict must not be relaxed"
    assert settings["warn_unreachable"] is True, "warn_unreachable catches the dead assertions"
    for key in SUPPRESSION_KEYS:
        assert key not in settings, (
            f"[tool.mypy] must not set {key}: a suppression lets the scope claim hold while "
            "files inside it go unchecked"
        )


def test_the_pre_commit_hook_runs_the_pyproject_scope() -> None:
    """The hook entry is bare and takes no filenames, so it runs the declared scope."""
    text = PRECOMMIT.read_text(encoding="utf-8")
    entry = _yaml_field(text, "- id: mypy", "entry")
    assert entry == "uv run --frozen mypy", f"the mypy hook entry is not bare: {entry!r}"
    assert _yaml_field(text, "- id: mypy", "pass_filenames") == "false", (
        "the hook must not be handed filenames: a one-file commit would otherwise check one file"
    )


def test_the_release_workflow_passes_no_path_to_mypy() -> None:
    """CI runs the declared scope, so the release proof and a local run are the same check."""
    command = _yaml_field(WORKFLOW.read_text(encoding="utf-8"), "- name: mypy", "run")
    assert _mypy_command_paths(command) == [], (
        f"the release mypy step narrows the scope itself: {command!r}"
    )


def test_no_document_or_hook_narrows_the_command_with_a_path() -> None:
    """One command, one scope: a trailing path argument would contradict pyproject.toml."""
    assert "`mypy`" in RELEASING.read_text(encoding="utf-8"), (
        "docs/releasing.md must name the bare `mypy` command"
    )
    sources = [
        *sorted((ROOT / "docs").rglob("*")),
        *sorted((ROOT / ".github/workflows").rglob("*")),
        PRECOMMIT,
    ]
    offenders = [
        f"{path.relative_to(ROOT)}: {match.group(0)}"
        for path in sources
        if path.is_file()
        for match in INVOCATION.finditer(path.read_text(encoding="utf-8", errors="replace"))
        if _narrowing_paths(match.group(0))
    ]
    assert not offenders, "mypy invoked with a path argument:\n  " + "\n  ".join(offenders)


def test_the_guard_would_catch_a_path_argument() -> None:
    """A positive control: the extractor must separate a path argument from a flag."""
    assert _mypy_command_paths("uv run --frozen --no-sync mypy src") == ["src"]
    assert _mypy_command_paths("uv run --frozen mypy") == []
