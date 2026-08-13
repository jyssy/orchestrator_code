"""
specialists.py — thin wrappers around REALMS models via litellm.
Each function maps to the best model for that task type.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from orchestrator.egress_guard import ModelEgressBlocked, RemoteTransmissionDenied
from orchestrator.model_gateway import ProviderFailure, completion
from orchestrator.results import ComponentResult, ResultStatus, diagnostic

load_dotenv(Path(__file__).parent.parent / ".env", override=False)  # shell vars win

_BASE_URL = os.getenv("REALMS_BASE_URL", "https://reallms.rescloud.iu.edu/direct/v1")
_API_KEY = os.getenv("REALMS_API_KEY", "")
_OFFLINE = os.getenv("OFFLINE_MODE", "false").lower() == "true"

# Model assignments
_CODING_MODEL = "Qwen3-Coder-Next"
_GENERAL_MODEL = "gemma-4-31B-it"   # 262K context — ideal for large file reads
_REASONING_MODEL = "gpt-oss-120b"   # heaviest tasks, judge passes

_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_LOCAL_CODING = os.getenv("OLLAMA_CODING_MODEL", "qwen2.5-coder:7b")


def _realms(model: str, messages: list[dict], **kwargs) -> str:
    """Call a REALMS model. Raises if offline mode is set or key is missing."""
    if _OFFLINE:
        raise ProviderFailure(
            ResultStatus.INVALID_CONFIGURATION,
            "remote_provider_disabled",
            "Remote model access is disabled by configuration.",
        )
    if not _API_KEY or _API_KEY == "your-key-here":
        raise ProviderFailure(
            ResultStatus.INVALID_CONFIGURATION,
            "remote_provider_key_missing",
            "Remote model authentication is not configured.",
        )
    response = completion(
        model=f"openai/{model}",
        messages=messages,
        api_base=_BASE_URL,
        api_key=_API_KEY,
        remote=True,
        **kwargs,
    )
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        raise ProviderFailure(
            ResultStatus.INTERNAL_FAILURE,
            "provider_malformed_response",
            "The model provider returned a malformed response.",
        ) from None


def _local(model: str, messages: list[dict]) -> str:
    """Call a local Ollama model through the guarded provider gateway."""
    response = completion(
        model=f"ollama/{model}",
        messages=messages,
        api_base=_OLLAMA_BASE,
        remote=False,
    )
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        raise ProviderFailure(
            ResultStatus.INTERNAL_FAILURE,
            "provider_malformed_response",
            "The model provider returned a malformed response.",
        ) from None


def _provider_result(component: str, exc: ProviderFailure) -> ComponentResult[str]:
    return ComponentResult(
        component,
        exc.status,
        code=exc.code,
        message=exc.safe_message,
        attempts=exc.attempts,
    )


def _internal_result(component: str) -> ComponentResult[str]:
    return ComponentResult(
        component,
        ResultStatus.INTERNAL_FAILURE,
        code="model_component_internal_failure",
        message="The model component failed unexpectedly.",
    )


def code(prompt: str, context: str = "") -> str:
    """Compatibility wrapper for the coding specialist."""
    result = code_result(prompt, context)
    if result.value is None:
        raise ProviderFailure(
            result.status,
            result.code,
            result.message,
            attempts=result.attempts,
        )
    return result.value


def code_result(prompt: str, context: str = "") -> ComponentResult[str]:
    """Coding specialist with an explicit remote/local fallback result."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    try:
        return ComponentResult(
            "specialist", ResultStatus.SUCCESS, _realms(_CODING_MODEL, messages)
        )
    except RemoteTransmissionDenied:
        # Only an explicit local-only policy triggers fallback. Scanner and
        # secret failures use a different error and remain fail-closed.
        try:
            value = _local(_LOCAL_CODING, messages)
        except ProviderFailure as exc:
            return _provider_result("specialist", exc)
        return ComponentResult(
            "specialist",
            ResultStatus.DEGRADED_SUCCESS,
            value,
            code="remote_denied_local_fallback",
            message="Repository policy required the local coding model.",
            warnings=(
                diagnostic(
                    "specialist",
                    "remote_denied_local_fallback",
                    "Repository policy required the local coding model.",
                ),
            ),
        )
    except ProviderFailure as remote_failure:
        if remote_failure.status is not ResultStatus.UNAVAILABLE_DEPENDENCY:
            return _provider_result("specialist", remote_failure)
        try:
            value = _local(_LOCAL_CODING, messages)
        except ProviderFailure as local_failure:
            return _provider_result("specialist", local_failure)
        return ComponentResult(
            "specialist",
            ResultStatus.DEGRADED_SUCCESS,
            value,
            code="remote_unavailable_local_fallback",
            message="The remote coding model was unavailable; the local model was used.",
            attempts=remote_failure.attempts,
            warnings=(
                diagnostic(
                    "specialist",
                    "remote_unavailable_local_fallback",
                    "The remote coding model was unavailable; the local model was used.",
                ),
            ),
        )
    except ModelEgressBlocked:
        raise
    except Exception:  # noqa: BLE001 - public status must not expose provider details
        return _internal_result("specialist")


def ops(prompt: str, context: str = "") -> str:
    """Ops/infra specialist — gemma-4-31B-it (large context for big playbooks)."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    return _realms(_GENERAL_MODEL, messages)


def ops_result(prompt: str, context: str = "") -> ComponentResult[str]:
    try:
        return ComponentResult("specialist", ResultStatus.SUCCESS, ops(prompt, context))
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        return _provider_result("specialist", exc)
    except Exception:  # noqa: BLE001 - public status must not expose provider details
        return _internal_result("specialist")


def reason(prompt: str, context: str = "") -> str:
    """Heavy reasoning — gpt-oss-120b."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    return _realms(_REASONING_MODEL, messages)


def reason_result(prompt: str, context: str = "") -> ComponentResult[str]:
    try:
        return ComponentResult(
            "specialist", ResultStatus.SUCCESS, reason(prompt, context)
        )
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        return _provider_result("specialist", exc)
    except Exception:  # noqa: BLE001 - public status must not expose provider details
        return _internal_result("specialist")


def summarize(prompt: str, context: str = "") -> str:
    """General summarization/explanation — gemma-4-31B-it."""
    return ops(prompt, context)


def summarize_result(prompt: str, context: str = "") -> ComponentResult[str]:
    return ops_result(prompt, context)
