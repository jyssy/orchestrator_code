"""Deterministic repository policy and explicit-context loading."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from orchestrator.security import sensitive_content_reason, sensitive_path_reason


_AGENT_FILENAMES = ("AGENTS.override.md", "AGENTS.md")
_MAX_AGENT_BYTES = 32_768
_MAX_CONTEXT_FILE_BYTES = 16_000
_MAX_EXPLICIT_CONTEXT_BYTES = 64_000


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def find_repo_root(start: str | Path | None) -> Path | None:
    """Find the nearest Git repository root without executing Git."""
    if start is None:
        return None

    path = Path(start).expanduser().resolve()
    current = path if path.is_dir() else path.parent

    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def resolve_repo_root(
    repo_root: str | Path | None,
    context_paths: list[str | Path] | None = None,
) -> Path | None:
    """Resolve an explicit repository root or infer it from the first context path."""
    if repo_root:
        root = Path(repo_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Repository root is not a directory: {root}")
        return root

    if context_paths:
        return find_repo_root(context_paths[0])
    return None


def _workspace_root_for(repo_root: Path) -> Path:
    configured = os.getenv("RAG_SOURCE_DIRS", "")
    if configured:
        workspace = Path(configured).expanduser().resolve()
        if workspace.is_dir() and _is_relative_to(repo_root, workspace):
            return workspace
    return repo_root


def load_agent_guidance(repo_root: Path, target_path: Path | None = None) -> str:
    """
    Load one AGENTS override/file per directory from workspace root to target.

    Later, more specific files appear later in the returned context and therefore
    take precedence, matching Codex's documented merge order.
    """
    workspace_root = _workspace_root_for(repo_root)
    target_directory = target_path if target_path and target_path.is_dir() else (
        target_path.parent if target_path else repo_root
    )
    target_directory = target_directory.resolve()

    if not _is_relative_to(target_directory, repo_root):
        target_directory = repo_root

    directories: list[Path] = []
    current = target_directory
    while True:
        directories.append(current)
        if current == workspace_root:
            break
        if current.parent == current or not _is_relative_to(current.parent, workspace_root):
            directories.append(workspace_root)
            break
        current = current.parent
    directories.reverse()

    sections: list[str] = []
    total_bytes = 0
    for directory in directories:
        selected = next(
            (directory / name for name in _AGENT_FILENAMES if (directory / name).is_file()),
            None,
        )
        if not selected:
            continue

        remaining = _MAX_AGENT_BYTES - total_bytes
        if remaining <= 0:
            break
        content = selected.read_text(errors="ignore")[:remaining]
        total_bytes += len(content.encode("utf-8"))
        sections.append(f"### Effective agent guidance: {selected}\n{content}")

    return "\n\n".join(sections)


def load_explicit_context(
    context_paths: list[str | Path],
    repo_root: Path | None,
) -> str:
    """Read explicitly requested safe files with path labels and bounded size."""
    sections: list[str] = []
    total_bytes = 0

    for raw_path in context_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Context path is not a file: {path}")
        if repo_root and not _is_relative_to(path, repo_root):
            raise ValueError(f"Context path is outside repository root: {path}")

        path_reason = sensitive_path_reason(path)
        if path_reason:
            raise ValueError(f"Refusing sensitive context file ({path_reason}): {path}")

        remaining = _MAX_EXPLICIT_CONTEXT_BYTES - total_bytes
        if remaining <= 0:
            break
        content = path.read_text(errors="ignore")[: min(_MAX_CONTEXT_FILE_BYTES, remaining)]
        content_reason = sensitive_content_reason(content)
        if content_reason:
            raise ValueError(f"Refusing sensitive context content ({content_reason}): {path}")

        total_bytes += len(content.encode("utf-8"))
        sections.append(f"### Explicit context: {path}\n{content}")

    return "\n\n".join(sections)


def load_git_state(repo_root: Path) -> str:
    """Collect the read-only Git state required by workspace AGENTS guidance."""
    if not (repo_root / ".git").exists():
        return ""

    commands = {
        "base commit": ["git", "rev-parse", "HEAD"],
        "working tree": ["git", "status", "--short", "--branch"],
        "submodules": ["git", "submodule", "status"],
        "diff check": ["git", "diff", "--check"],
    }
    sections: list[str] = []

    for label, command in commands.items():
        try:
            result = subprocess.run(
                command,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            output = (result.stdout + result.stderr).strip() or "(clean/no output)"
            sections.append(f"#### {label}\n{output[:12_000]}")
        except (OSError, subprocess.SubprocessError) as exc:
            sections.append(f"#### {label}\nUnable to inspect: {exc}")

    return "### Read-only repository state\n" + "\n\n".join(sections)
