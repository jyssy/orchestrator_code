"""
pipeline.py — main orchestration: router → RAG → specialist → judge.
"""

from pathlib import Path

from orchestrator.context import (
    load_agent_guidance,
    load_explicit_context,
    load_git_state,
    resolve_repo_root,
)
from orchestrator.router import classify
from orchestrator.rag import retrieve_context
from orchestrator.specialists import code, ops, reason, summarize
from orchestrator.judge import critique_and_revise

_PLAN_SYSTEM = """You are a careful technical planner working in a multi-repository
infrastructure and Django workspace.

Given a task, produce a structured plan with these sections:

## Scope
- Repository/file(s) affected (read from context — do not guess)
- Base assumption (what you are treating as current state)

## Proposed changes
List each change as a bullet: what file, what section, what will change and why.

## Allowed and prohibited paths
List allowed write paths, prohibited paths, frozen interfaces, submodules, and
upstream/downstream repositories that must remain unchanged.

## What will NOT change
List explicitly what is out of scope.

## Agent-executable checks
List only checks the effective AGENTS.md guidance permits an agent to run.

## Human-only pending checks
List exact commands that policy prohibits the agent from running. Mark every
one pending for an authorized human; never describe it as already executed.

## Human gates
List any actions that require explicit approval before proceeding
(migrations, deployments, pushes, vault, production changes, service restarts).

## Risks and assumptions
List unresolved risks, assumptions made, and any information missing.

## Required handoff
State what the implementation handoff must report: files changed, checks run and
results, checks not run, failures, assumptions, and unresolved risks.

The effective AGENTS.md guidance in context is authoritative. Never propose that
an agent run a prohibited command. Preserve all pre-existing tracked, untracked,
submodule, and nested-repository work. Do NOT implement anything. Only plan."""


def _normalize_context_paths(
    context_path: str | None,
    context_paths: list[str] | None,
) -> list[str]:
    paths = list(context_paths or [])
    if context_path and context_path not in paths:
        paths.insert(0, context_path)
    return paths


def _build_context(
    prompt: str,
    *,
    repo_root: str | None,
    context_path: str | None,
    context_paths: list[str] | None,
) -> tuple[str, Path | None]:
    paths = _normalize_context_paths(context_path, context_paths)
    resolved_root = resolve_repo_root(repo_root, paths)
    target = Path(paths[0]).expanduser().resolve() if paths else resolved_root

    sections: list[str] = []
    if resolved_root:
        guidance = load_agent_guidance(resolved_root, target_path=target)
        if guidance:
            sections.append(guidance)
        git_state = load_git_state(resolved_root)
        if git_state:
            sections.append(git_state)

    if paths:
        sections.append(load_explicit_context(paths, resolved_root))

    retrieved = retrieve_context(
        prompt,
        repo_root=str(resolved_root) if resolved_root else None,
    )
    if retrieved:
        sections.append(retrieved)

    return "\n\n---\n\n".join(section for section in sections if section), resolved_root


def plan(
    prompt: str,
    context_path: str | None = None,
    *,
    context_paths: list[str] | None = None,
    repo_root: str | None = None,
) -> str:
    """
    Produce a structured plan for a task without executing it.
    Returns the plan text for human review.
    """
    context, _ = _build_context(
        prompt,
        repo_root=repo_root,
        context_path=context_path,
        context_paths=context_paths,
    )

    plan_prompt = f"{_PLAN_SYSTEM}\n\nTask:\n{prompt}"
    return reason(plan_prompt, context=context)


def run(
    prompt: str,
    context_path: str | None = None,
    judge_enabled: bool | None = None,
    *,
    context_paths: list[str] | None = None,
    repo_root: str | None = None,
) -> dict:
    """
    Orchestrate a full request through the pipeline.

    Returns a dict with:
      task_type, context_used (bool), draft, final
    """
    # 1. Classify
    task_type = classify(prompt)

    # 2. Load effective AGENTS guidance, safe explicit files, Git state, and RAG
    context, resolved_root = _build_context(
        prompt,
        repo_root=repo_root,
        context_path=context_path,
        context_paths=context_paths,
    )

    # 3. Route to specialist
    if task_type == "coding":
        draft = code(prompt, context=context)
    elif task_type == "ops":
        draft = ops(prompt, context=context)
    elif task_type == "search":
        draft = summarize(prompt, context=context)
    else:
        draft = reason(prompt, context=context)

    # 4. Judge pass (critique + optional revision)
    final = critique_and_revise(
        prompt,
        draft,
        enabled=judge_enabled,
        context=context,
    )

    return {
        "task_type": task_type,
        "context_used": bool(context),
        "repo_root": str(resolved_root) if resolved_root else None,
        "draft": draft,
        "final": final,
    }
