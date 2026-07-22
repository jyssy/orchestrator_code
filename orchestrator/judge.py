"""
judge.py — single critique pass via gpt-oss-120b, then one revision.
Skipped when JUDGE_ENABLED=false or when the first answer is already confident.
"""

import os
from orchestrator.specialists import reason

_JUDGE_ENABLED = os.getenv("JUDGE_ENABLED", "true").lower() == "true"

_CRITIQUE_SYSTEM = """You are a rigorous code and technical reviewer.
Given a user request and a draft answer, identify:
1. Factual errors or incorrect code
2. Missing edge cases or security issues
3. Unnecessary complexity

Be concise. If the answer is correct and complete, reply with exactly: LGTM"""

_REVISE_SYSTEM = """You are an expert technical assistant.
Revise the draft answer based on the critique provided.
Return only the improved answer."""


def critique_and_revise(prompt: str, draft: str) -> str:
    """
    Run a critique pass on draft. If issues are found, produce one revision.
    Returns the final answer (revised or original).
    """
    if not _JUDGE_ENABLED:
        return draft

    critique_prompt = f"User request:\n{prompt}\n\nDraft answer:\n{draft}"
    critique = reason(critique_prompt, context=f"system: {_CRITIQUE_SYSTEM}")

    if critique.strip().upper().startswith("LGTM"):
        return draft

    revision_prompt = (
        f"User request:\n{prompt}\n\n"
        f"Draft answer:\n{draft}\n\n"
        f"Critique:\n{critique}\n\n"
        "Produce the corrected answer."
    )
    return reason(revision_prompt, context=f"system: {_REVISE_SYSTEM}")
