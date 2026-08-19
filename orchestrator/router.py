"""
router.py — classifies a prompt into a task type using the local Ollama model.
Falls back to keyword heuristics if Ollama is unavailable.
"""

import os
from typing import Literal

from orchestrator.egress_guard import ModelEgressBlocked
from orchestrator.model_gateway import ProviderFailure, ollama_generate
from orchestrator.model_roles import router_model
from orchestrator.results import ComponentResult, ResultStatus, diagnostic

TaskType = Literal["coding", "ops", "general", "search"]

_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_SYSTEM_PROMPT = """You are a task classifier. Given a user prompt, output exactly one word:
- coding    (writing, debugging, refactoring, or reviewing code)
- ops       (infrastructure, ansible, terraform, CI/CD, server administration)
- search    (finding information within a codebase or docs)
- general   (anything else)

Output only the single word, lowercase, no punctuation."""


def _keyword_fallback(prompt: str) -> TaskType:
    """Heuristic classification when Ollama is offline."""
    lower = prompt.lower()
    coding_keywords = {"python", "function", "class", "bug", "refactor", "test", "import", "code", "script"}
    ops_keywords = {"ansible", "terraform", "playbook", "role", "server", "deploy", "pipeline", "yaml", "task", "host"}
    search_keywords = {"find", "where", "search", "which file", "locate", "show me"}

    if any(k in lower for k in ops_keywords):
        return "ops"
    if any(k in lower for k in coding_keywords):
        return "coding"
    if any(k in lower for k in search_keywords):
        return "search"
    return "general"


def classify_result(prompt: str) -> ComponentResult[TaskType]:
    """Return task classification with visible local-router degradation."""
    model = router_model()
    try:
        response = ollama_generate(
            base_url=_OLLAMA_BASE,
            model=model,
            system=_SYSTEM_PROMPT,
            prompt=prompt,
            options={"temperature": 0, "num_predict": 5},
            timeout=10.0,
        )
        raw = response.json().get("response", "").strip().lower().split()[0]
        if raw in ("coding", "ops", "search", "general"):
            return ComponentResult(
                "router", ResultStatus.SUCCESS, raw, model=model
            )
        return ComponentResult(
            "router",
            ResultStatus.DEGRADED_SUCCESS,
            _keyword_fallback(prompt),
            code="router_invalid_response",
            message="Local routing returned an invalid category; heuristics were used.",
            warnings=(
                diagnostic(
                    "router",
                    "router_invalid_response",
                    "Local routing returned an invalid category; heuristics were used.",
                ),
            ),
        )
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        return ComponentResult(
            "router",
            ResultStatus.DEGRADED_SUCCESS,
            _keyword_fallback(prompt),
            code="router_heuristic_fallback",
            message="Local routing was unavailable; heuristics were used.",
            attempts=exc.attempts,
            warnings=(
                diagnostic(
                    "router",
                    "router_heuristic_fallback",
                    "Local routing was unavailable; heuristics were used.",
                ),
            ),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return ComponentResult(
            "router",
            ResultStatus.DEGRADED_SUCCESS,
            _keyword_fallback(prompt),
            code="router_malformed_response",
            message="Local routing response was malformed; heuristics were used.",
            warnings=(
                diagnostic(
                    "router",
                    "router_malformed_response",
                    "Local routing response was malformed; heuristics were used.",
                ),
            ),
        )
    except Exception:  # noqa: BLE001 - local fallback is explicit and content-safe
        return ComponentResult(
            "router",
            ResultStatus.DEGRADED_SUCCESS,
            _keyword_fallback(prompt),
            code="router_internal_fallback",
            message="Local routing failed unexpectedly; heuristics were used.",
            warnings=(
                diagnostic(
                    "router",
                    "router_internal_fallback",
                    "Local routing failed unexpectedly; heuristics were used.",
                ),
            ),
        )


def classify(prompt: str) -> TaskType:
    """Compatibility wrapper returning only the selected task type."""
    result = classify_result(prompt)
    if result.value is None:
        raise RuntimeError("Routing did not produce a task type")
    return result.value
