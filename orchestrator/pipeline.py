"""
pipeline.py — main orchestration: router → RAG → specialist → judge.
"""

import os
from orchestrator.router import classify
from orchestrator.rag import retrieve_context
from orchestrator.specialists import code, ops, reason, summarize
from orchestrator.judge import critique_and_revise


def run(prompt: str, context_path: str | None = None) -> dict:
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
    final = critique_and_revise(prompt, draft)

    return {
        "task_type": task_type,
        "context_used": bool(rag_context),
        "draft": draft,
        "final": final,
    }
