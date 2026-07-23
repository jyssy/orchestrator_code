"""Reusable prompts and launch arguments for the daily coding workflow."""

from __future__ import annotations

import shutil
from enum import Enum
from pathlib import Path

from orchestrator.context import find_repo_root
from orchestrator.security import sensitive_content_reason


class Executor(str, Enum):
    CODEX = "codex"
    COPILOT = "copilot"


def resolve_target_repo(value: str | Path | None) -> Path:
    """Resolve a path inside a Git repository to that repository's root."""
    candidate = Path(value or Path.cwd()).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"Target path is not a directory: {candidate}")

    repo_root = find_repo_root(candidate)
    if repo_root is None:
        raise ValueError(f"Target path is not inside a Git repository: {candidate}")
    return repo_root


def _validate_task(task: str) -> str:
    normalized = task.strip()
    if not normalized:
        raise ValueError("Task must not be empty")

    secret_reason = sensitive_content_reason(normalized)
    if secret_reason:
        raise ValueError(
            f"Task appears to contain prohibited secret material ({secret_reason})"
        )
    return normalized


def build_codex_prompt(task: str, repo_root: Path) -> str:
    """Build the interactive Codex prompt that enforces the approval boundary."""
    task = _validate_task(task)
    return f"""Use the orchestrator as architect/reviewer and act as the code executor.

Target repository: {repo_root}
Task: {task}

Workflow:
1. Read the effective AGENTS.md guidance and inspect the repository read-only.
2. Call the orchestrator MCP tool `plan_task` with the task above and
   repo_root="{repo_root}".
3. Show me the plan, then STOP and wait for a separate approval message. Do not
   edit files or run mutating commands before that approval.
4. After approval, call `ask_orchestrator` with the same task and exact same
   repo_root. Evaluate its advice rather than applying it blindly.
5. Implement only the approved scope. Preserve all unrelated tracked, untracked,
   submodule, and nested-repository work.
6. Run only checks permitted by the effective AGENTS.md. Finish by showing the
   final diff and reporting files changed, checks passed, checks not run,
   failures, assumptions, and unresolved risks.

If either orchestrator tool is unavailable, report that and stop. Do not commit,
push, merge, tag, release, deploy, access secrets, run infrastructure plans or
playbooks, perform migrations, or restart services unless I separately and
explicitly authorize the exact action."""


def build_copilot_prompt(task: str, repo_root: Path) -> str:
    """Build the equivalent VS Code Copilot Agent-mode prompt."""
    task = _validate_task(task)
    return f"""Work only in this repository: {repo_root}

Task: {task}

Use `#plan_task` with repo_root="{repo_root}" and show me the plan without
editing. Then STOP and wait for a separate approval message. After approval, use
`#ask_orchestrator` with the same task and exact same repo_root, evaluate its
advice, implement only the approved scope, and run checks permitted by the
effective AGENTS.md.

Preserve unrelated work. In the handoff, report files changed, checks passed,
checks not run, failures, assumptions, and unresolved risks. Do not commit,
push, merge, tag, release, deploy, access secrets, run infrastructure plans or
playbooks, perform migrations, or restart services without separate explicit
authorization."""


def build_codex_command(task: str, repo_root: Path) -> list[str]:
    """Return a shell-free Codex launch command with conservative permissions."""
    executable = shutil.which("codex")
    if executable is None:
        raise ValueError("Codex CLI was not found on PATH")

    return [
        executable,
        "-C",
        str(repo_root),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-request",
        build_codex_prompt(task, repo_root),
    ]
