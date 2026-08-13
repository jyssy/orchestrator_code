"""The only module permitted to invoke local or remote model providers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import litellm

from orchestrator.egress_guard import guard_payload


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
    return litellm.completion(**request)


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
    return litellm.embedding(
        model=model,
        input=texts,
        api_base=api_base,
        api_key=api_key,
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
    response = httpx.post(
        f"{base_url}/api/generate",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


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
    response = httpx.post(
        f"{base_url}/rerank",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response
