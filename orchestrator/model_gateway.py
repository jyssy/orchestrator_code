"""The only module permitted to invoke local or remote model providers."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import litellm

from orchestrator.egress_guard import guard_payload
from orchestrator.results import ResultStatus

_TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_AUTH_HTTP_STATUSES = {401, 403}
_INVALID_HTTP_STATUSES = {400, 404, 409, 422}


@dataclass(frozen=True)
class ProviderFailure(RuntimeError):
    """Sanitized provider failure; never stores the original exception."""

    status: ResultStatus
    code: str
    safe_message: str
    attempts: int = 1
    retryable: bool = False

    def __str__(self) -> str:
        return self.safe_message


def _retry_settings() -> tuple[int, float]:
    try:
        attempts = int(os.getenv("MODEL_REMOTE_MAX_ATTEMPTS", "3"))
        delay = float(os.getenv("MODEL_RETRY_BASE_SECONDS", "0.25"))
    except ValueError:
        raise ProviderFailure(
            ResultStatus.INVALID_CONFIGURATION,
            "invalid_retry_configuration",
            "Remote retry configuration is invalid.",
        ) from None
    if attempts < 1 or attempts > 5 or delay < 0 or delay > 5:
        raise ProviderFailure(
            ResultStatus.INVALID_CONFIGURATION,
            "invalid_retry_configuration",
            "Remote retry configuration is outside supported bounds.",
        )
    return attempts, delay


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _classify_provider_exception(exc: Exception) -> ProviderFailure:
    status_code = _http_status(exc)
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)) or (
        status_code in _TRANSIENT_HTTP_STATUSES
    ):
        return ProviderFailure(
            ResultStatus.UNAVAILABLE_DEPENDENCY,
            "provider_temporarily_unavailable",
            "The model provider is temporarily unavailable.",
            retryable=True,
        )
    if status_code in _AUTH_HTTP_STATUSES:
        return ProviderFailure(
            ResultStatus.INVALID_CONFIGURATION,
            "provider_authentication_failed",
            "The model provider rejected its authentication configuration.",
        )
    if status_code in _INVALID_HTTP_STATUSES:
        return ProviderFailure(
            ResultStatus.INVALID_INPUT,
            "provider_request_rejected",
            "The model provider rejected the request.",
        )
    return ProviderFailure(
        ResultStatus.INTERNAL_FAILURE,
        "provider_internal_failure",
        "The model provider failed unexpectedly.",
    )


def _invoke_provider(operation: Callable[[], Any], *, remote: bool) -> Any:
    """Invoke a provider with bounded retries only for classified transient errors."""
    max_attempts, base_delay = _retry_settings() if remote else (1, 0.0)
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except ProviderFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - provider libraries vary
            failure = _classify_provider_exception(exc)
            if not failure.retryable or attempt == max_attempts:
                raise ProviderFailure(
                    failure.status,
                    failure.code,
                    failure.safe_message,
                    attempts=attempt,
                    retryable=failure.retryable,
                ) from None
            time.sleep(base_delay * (2 ** (attempt - 1)))
    raise AssertionError("unreachable provider retry state")


def _post_json(
    url: str,
    *,
    payload: dict[str, Any],
    timeout: float,
    headers: dict[str, str] | None = None,
    remote: bool,
) -> Any:
    def operation() -> Any:
        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    return _invoke_provider(operation, remote=remote)


def _serialize_model_payload(payload: Any) -> str:
    """Serialize only JSON-compatible model-bound data; fail on ambiguity."""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Model payload contains unsupported data") from exc


def completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_base: str,
    api_key: str | None = None,
    remote: bool,
    **kwargs: Any,
) -> Any:
    """Guard a completed chat payload and invoke LiteLLM."""
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_base": api_base,
        **kwargs,
    }
    if api_key is not None:
        request["api_key"] = api_key
    model_payload = {key: value for key, value in request.items() if key != "api_key"}
    guard_payload(
        [_serialize_model_payload(model_payload)],
        source="assembled completion payload",
        remote=remote,
    )
    return _invoke_provider(lambda: litellm.completion(**request), remote=remote)


def embedding(
    *,
    model: str,
    texts: list[str],
    api_base: str,
    api_key: str,
) -> Any:
    """Guard every embedding input and invoke LiteLLM."""
    guard_payload(
        [_serialize_model_payload({"model": model, "input": texts})],
        source="assembled embedding payload",
        remote=True,
    )
    return _invoke_provider(
        lambda: litellm.embedding(
            model=model,
            input=texts,
            api_base=api_base,
            api_key=api_key,
        ),
        remote=True,
    )


def ollama_generate(
    *,
    base_url: str,
    model: str,
    system: str,
    prompt: str,
    options: dict[str, Any],
    timeout: float,
) -> Any:
    """Guard and send a local Ollama generation request."""
    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }
    guard_payload(
        [_serialize_model_payload(payload)],
        source="assembled local routing payload",
        remote=False,
    )
    return _post_json(
        f"{base_url}/api/generate",
        payload=payload,
        timeout=timeout,
        remote=False,
    )


def rerank(
    *,
    base_url: str,
    api_key: str,
    model: str,
    query: str,
    documents: list[str],
    top_n: int,
    timeout: float,
) -> Any:
    """Guard query and candidate documents, then call the remote reranker."""
    payload = {
        "model": model,
        "query": query,
        "documents": documents,
        "top_n": top_n,
    }
    guard_payload(
        [_serialize_model_payload(payload)],
        source="assembled reranking payload",
        remote=True,
    )
    return _post_json(
        f"{base_url}/rerank",
        headers={"Authorization": f"Bearer {api_key}"},
        payload=payload,
        timeout=timeout,
        remote=True,
    )
