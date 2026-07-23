"""
pipeline.py — main orchestration: router → RAG → specialist → judge.
"""

import os
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

## What will NOT change
List explicitly what is out of scope.

## Checks required before executing
List the exact commands to run for validation (syntax checks, dry-runs, etc.)

## Human gates
List any actions that require explicit approval before proceeding
(migrations, deployments, pushes, vault, production changes, service restarts).

## Risks and assumptions
List unresolved risks, assumptions made, and any information missing.

Do NOT implement anything. Only plan."""


def plan(prompt: str, context_path: str | None = None) -> str:
    """
    Produce a structured plan for a task without executing it.
    Returns the plan text for human review.
    """
    rag_context = retrieve_context(prompt)
    if context_path:
        try:
            from pathlib import Path
            extra = Path(context_path).read_text(errors="ignore")[:8000]
            rag_context = f"{extra}\n\n{rag_context}".strip()
        except Exception:
            pass

    plan_prompt = f"{_PLAN_SYSTEM}\n\nTask:\n{prompt}"
    return reason(plan_prompt, context=rag_context)


def run(
    prompt: str,
    context_path: str | None = None,
    judge_enabled: bool | None = None,
) -> dict:
    """
    Orchestrate a full request through the pipeline.

    Returns a dict with:
      task_type, context_used (bool), draft, final
    """
    # 1. Classify
    task_type = classify(prompt)

    # 2. Retrieve context (RAG)
    rag_context = retrieve_context(prompt)
    if context_path:
        try:
            from pathlib import Path
            extra = Path(context_path).read_text(errors="ignore")[:8000]
            rag_context = f"{extra}\n\n{rag_context}".strip()
        except Exception:
            pass

    # 3. Route to specialist
    if task_type == "coding":
        draft = code(prompt, context=rag_context)
    elif task_type == "ops":
        draft = ops(prompt, context=rag_context)
    elif task_type == "search":
        draft = summarize(prompt, context=rag_context)
    else:
        draft = reason(prompt, context=rag_context)

    # 4. Judge pass (critique + optional revision)
    final = critique_and_revise(prompt, draft, enabled=judge_enabled)

    return {
        "task_type": task_type,
        "context_used": bool(rag_context),
        "draft": draft,
        "final": final,
    }
