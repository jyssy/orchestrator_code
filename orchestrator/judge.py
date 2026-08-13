"""
judge.py — single critique pass via gpt-oss-120b, then one revision.
Skipped when JUDGE_ENABLED=false or when the first answer is already confident.
"""

import os

from orchestrator.egress_guard import ModelEgressBlocked
from orchestrator.model_gateway import ProviderFailure
from orchestrator.results import ComponentResult, ResultStatus, diagnostic
from orchestrator.specialists import reason

_CRITIQUE_SYSTEM = """You are a rigorous code and technical reviewer.
Given a user request and a draft answer, identify:
1. Factual errors or incorrect code
2. Missing edge cases or security issues
3. Unnecessary complexity

Be concise. If the answer is correct and complete, reply with exactly: LGTM"""

_REVISE_SYSTEM = """You are an expert technical assistant.
Revise the draft answer based on the critique provided.
Return only the improved answer."""


def _fallback_result(
    draft: str,
    failure: ProviderFailure | None,
    *,
    revision: bool,
) -> ComponentResult[str]:
    stage = "revision" if revision else "judge"
    if failure is None:
        code = f"{stage}_internal_failure"
        message = (
            "Revision failed unexpectedly; the specialist draft was retained."
            if revision
            else "The judge pass failed unexpectedly; the specialist draft was retained."
        )
        attempts = 1
    else:
        details = {
            ResultStatus.UNAVAILABLE_DEPENDENCY: (
                f"{stage}_unavailable",
                (
                    "Revision was unavailable; the specialist draft was retained."
                    if revision
                    else "The judge pass was unavailable; the specialist draft was retained."
                ),
            ),
            ResultStatus.INVALID_CONFIGURATION: (
                f"{stage}_invalid_configuration",
                (
                    "Revision configuration was invalid; the specialist draft was retained."
                    if revision
                    else "Judge configuration was invalid; the specialist draft was retained."
                ),
            ),
            ResultStatus.INVALID_INPUT: (
                f"{stage}_request_rejected",
                (
                    "The revision request was rejected; the specialist draft was retained."
                    if revision
                    else "The judge request was rejected; the specialist draft was retained."
                ),
            ),
            ResultStatus.INTERNAL_FAILURE: (
                f"{stage}_internal_failure",
                (
                    "Revision failed unexpectedly; the specialist draft was retained."
                    if revision
                    else "The judge pass failed unexpectedly; the specialist draft was retained."
                ),
            ),
        }
        code, message = details.get(
            failure.status,
            (
                f"{stage}_failed",
                (
                    "Revision could not complete; the specialist draft was retained."
                    if revision
                    else "The judge pass could not complete; the specialist draft was retained."
                ),
            ),
        )
        attempts = failure.attempts
    return ComponentResult(
        "judge",
        ResultStatus.DEGRADED_SUCCESS,
        draft,
        code=code,
        message=message,
        attempts=attempts,
        warnings=(diagnostic("judge", code, message),),
    )


def critique_and_revise(
    prompt: str,
    draft: str,
    enabled: bool | None = None,
    context: str = "",
) -> str:
    """Compatibility wrapper returning the original or revised answer."""
    result = critique_and_revise_result(
        prompt,
        draft,
        enabled=enabled,
        context=context,
    )
    if result.value is None:
        raise ProviderFailure(
            result.status,
            result.code,
            result.message,
            attempts=result.attempts,
        )
    return result.value


def critique_and_revise_result(
    prompt: str,
    draft: str,
    enabled: bool | None = None,
    context: str = "",
) -> ComponentResult[str]:
    """
    Run a critique pass on draft. If issues are found, produce one revision.
    Returns the final answer (revised or original).
    """
    if enabled is None:
        enabled = os.getenv("JUDGE_ENABLED", "true").lower() == "true"

    if not enabled:
        return ComponentResult(
            "judge",
            ResultStatus.SUCCESS,
            draft,
            code="judge_disabled",
            message="The judge pass was disabled for this request.",
        )

    critique_prompt = f"User request:\n{prompt}\n\nDraft answer:\n{draft}"
    critique_context = f"system: {_CRITIQUE_SYSTEM}"
    if context:
        critique_context = (
            f"{critique_context}\n\nEffective policy and context:\n{context}"
        )
    try:
        critique = reason(critique_prompt, context=critique_context)
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        return _fallback_result(draft, exc, revision=False)
    except Exception:  # noqa: BLE001 - retain draft with a content-safe warning
        return _fallback_result(draft, None, revision=False)

    if critique.strip().upper().startswith("LGTM"):
        return ComponentResult("judge", ResultStatus.SUCCESS, draft)

    revision_prompt = (
        f"User request:\n{prompt}\n\n"
        f"Draft answer:\n{draft}\n\n"
        f"Critique:\n{critique}\n\n"
        "Produce the corrected answer."
    )
    revision_context = f"system: {_REVISE_SYSTEM}"
    if context:
        revision_context = (
            f"{revision_context}\n\nEffective policy and context:\n{context}"
        )
    try:
        revised = reason(revision_prompt, context=revision_context)
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        return _fallback_result(draft, exc, revision=True)
    except Exception:  # noqa: BLE001 - retain draft with a content-safe warning
        return _fallback_result(draft, None, revision=True)
    return ComponentResult("judge", ResultStatus.SUCCESS, revised)
