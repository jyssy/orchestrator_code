"""Deterministic repository policy and explicit-context loading."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from orchestrator.models import PolicyIdentity, RepositorySnapshot
from orchestrator.security import (
    MODEL_EGRESS_POLICY_FILENAME,
    excluded_directory,
    load_model_egress_policy,
    model_egress_policy_path,
    sensitive_content_reason,
    sensitive_path_reason,
)

_AGENT_FILENAMES = ("AGENTS.override.md", "AGENTS.md")
_MAX_AGENT_BYTES = 32_768
_MAX_CONTEXT_FILE_BYTES = 16_000
_MAX_EXPLICIT_CONTEXT_BYTES = 64_000
_MAX_EFFECTIVE_CONSTRAINT_BYTES = 16_000


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


def _agent_guidance_files(
    repo_root: Path, target_path: Path | None = None
) -> list[Path]:
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

    selected_files: list[Path] = []
    for directory in directories:
        selected = next(
            (directory / name for name in _AGENT_FILENAMES if (directory / name).is_file()),
            None,
        )
        if selected:
            selected_files.append(selected)
    return selected_files


def load_agent_guidance(repo_root: Path, target_path: Path | None = None) -> str:
    """
    Load one AGENTS override/file per directory from workspace root to target.

    Later, more specific files appear later in the returned context and therefore
    take precedence, matching Codex's documented merge order.
    """
    sections: list[str] = []
    total_bytes = 0
    for selected in _agent_guidance_files(repo_root, target_path):
        remaining = _MAX_AGENT_BYTES - total_bytes
        if remaining <= 0:
            break
        content = selected.read_text(errors="ignore")[:remaining]
        total_bytes += len(content.encode("utf-8"))
        sections.append(f"### Effective agent guidance: {selected}\n{content}")

    return "\n\n".join(sections)


def sanitize_effective_constraints(value: str | None) -> str:
    """Validate bounded caller constraints before including them in model context."""
    constraints = (value or "").strip()
    if len(constraints.encode("utf-8")) > _MAX_EFFECTIVE_CONSTRAINT_BYTES:
        raise ValueError("Effective constraints exceed 16,000 bytes")
    content_reason = sensitive_content_reason(constraints)
    if content_reason:
        raise ValueError(
            "Effective constraints contain prohibited secret material "
            f"({content_reason})"
        )
    return constraints


def load_policy_identity(
    repo_root: Path,
    target_path: Path | None = None,
    effective_constraints: str | None = None,
    model_egress_roots: list[Path] | None = None,
) -> tuple[str, PolicyIdentity, str]:
    """Return guidance, its deterministic identity, and sanitized constraints."""
    constraints = sanitize_effective_constraints(effective_constraints)
    guidance = load_agent_guidance(repo_root, target_path)
    guidance_sources = tuple(
        str(path.resolve()) for path in _agent_guidance_files(repo_root, target_path)
    )
    policy_roots = model_egress_roots or [repo_root]
    egress_policies: list[tuple[str, str]] = []
    for policy_root in dict.fromkeys(root.resolve() for root in policy_roots):
        policy_path = model_egress_policy_path(policy_root)
        policy_text, _ = load_model_egress_policy(policy_root)
        egress_policies.append((str(policy_path), policy_text))
    # Keep the policy path in the identity even when it does not yet exist so
    # creating an explicit remote opt-in invalidates an already approved plan.
    sources = (*guidance_sources, *(path for path, _ in egress_policies))
    payload = json.dumps(
        {
            "guidance": guidance,
            "model_egress_policies": egress_policies,
            "sources": sources,
            "constraints": constraints,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = PolicyIdentity(
        fingerprint=hashlib.sha256(payload.encode()).hexdigest(),
        sources=sources,
    )
    return guidance, identity, constraints


def reload_policy_identity(
    sources: tuple[str, ...], effective_constraints: str | None
) -> PolicyIdentity:
    """Recompute an existing plan's policy identity from the same sources."""
    constraints = sanitize_effective_constraints(effective_constraints)
    sections: list[str] = []
    egress_policies: list[tuple[str, str]] = []
    total_bytes = 0
    for raw_path in sources:
        source_path = Path(raw_path).expanduser()
        if source_path.name == MODEL_EGRESS_POLICY_FILENAME:
            policy_text, _ = load_model_egress_policy(source_path.parent.resolve())
            egress_policies.append((str(source_path.resolve()), policy_text))
            continue
        path = source_path.resolve()
        if not path.is_file():
            raise ValueError(f"Policy source is missing: {path}")
        remaining = _MAX_AGENT_BYTES - total_bytes
        if remaining <= 0:
            break
        content = path.read_text(errors="ignore")[:remaining]
        total_bytes += len(content.encode("utf-8"))
        sections.append(f"### Effective agent guidance: {path}\n{content}")
    guidance = "\n\n".join(sections)
    payload = json.dumps(
        {
            "guidance": guidance,
            "model_egress_policies": egress_policies,
            "sources": sources,
            "constraints": constraints,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return PolicyIdentity(
        fingerprint=hashlib.sha256(payload.encode()).hexdigest(),
        sources=sources,
    )


def _run_git(repo_root: Path, arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Unable to inspect repository state: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or "git returned a non-zero status"
        raise ValueError(f"Unable to inspect repository state: {detail}")
    return result.stdout


def _status_paths(status: str) -> tuple[str, ...]:
    """Extract both sides of Git rename/copy records from porcelain-v1 output."""
    records = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise ValueError("Unexpected Git status output")
        state = record[:2]
        paths.append(record[3:])
        if "R" in state or "C" in state:
            if index >= len(records) or not records[index]:
                raise ValueError("Incomplete Git rename/copy status")
            paths.append(records[index])
            index += 1
    return tuple(sorted(set(paths)))


def _nested_git_roots(source: Path) -> tuple[Path, ...]:
    """Return the source and discoverable nested Git roots without following links."""
    roots = [source]
    for directory, directory_names, _ in os.walk(source, followlinks=False):
        current = Path(directory)
        if current != source and (current / ".git").exists():
            roots.append(current)
            directory_names[:] = []
            continue
        directory_names[:] = [
            name
            for name in directory_names
            if name != ".git"
            and not (current / name).is_symlink()
            and not excluded_directory(current / name)
        ]
    return tuple(roots)


def capture_repository_snapshot(repo_root: Path) -> RepositorySnapshot:
    """Capture Git-visible state without reading working-tree file contents."""
    root = repo_root.expanduser().resolve()
    if not (root / ".git").exists():
        raise ValueError(f"Repository root is not a Git repository: {root}")
    base_commit = _run_git(root, ["rev-parse", "HEAD"]).strip()
    repository_states: list[tuple[str, str, str]] = []
    changed: set[str] = set()
    for git_root in _nested_git_roots(root):
        prefix = "" if git_root == root else git_root.relative_to(root).as_posix()
        nested_commit = _run_git(git_root, ["rev-parse", "HEAD"]).strip()
        status = _run_git(
            git_root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
        repository_states.append((prefix, nested_commit, status))
        for path in _status_paths(status):
            changed.add(f"{prefix}/{path}" if prefix else path)
    changed_paths = tuple(sorted(changed))
    metadata: list[tuple[str, int | None, int | None, int | None]] = []
    for relative in changed_paths:
        try:
            stat = (root / relative).lstat()
            metadata.append((relative, stat.st_mode, stat.st_size, stat.st_mtime_ns))
        except OSError:
            metadata.append((relative, None, None, None))
    fingerprint_payload = json.dumps(
        {"repositories": repository_states, "metadata": metadata},
        sort_keys=True,
        separators=(",", ":"),
    )
    return RepositorySnapshot(
        repo_root=str(root),
        base_commit=base_commit,
        working_tree_fingerprint=hashlib.sha256(
            fingerprint_payload.encode()
        ).hexdigest(),
        changed_paths=changed_paths,
    )


def capture_path_metadata(
    repo_root: Path, paths: tuple[str, ...]
) -> dict[str, tuple[int | None, int | None, int | None]]:
    """Capture non-content metadata used to detect edits to pre-existing changes."""
    root = repo_root.expanduser().resolve()
    metadata: dict[str, tuple[int | None, int | None, int | None]] = {}
    for relative in paths:
        try:
            stat = (root / relative).lstat()
            metadata[relative] = (stat.st_mode, stat.st_size, stat.st_mtime_ns)
        except OSError:
            metadata[relative] = (None, None, None)
    return metadata


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
