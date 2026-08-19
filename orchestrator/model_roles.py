"""Authoritative model assignments and human-readable orchestration labels."""

from __future__ import annotations

import os

_DEFAULT_CODING_MODEL = "Qwen3-Coder-Next"
_DEFAULT_GENERAL_MODEL = "gemma-4-31B-it"
_DEFAULT_REASONING_MODEL = "gpt-oss-120b"
_DEFAULT_LOCAL_CODING_MODEL = "qwen2.5-coder:7b"
_DEFAULT_ROUTER_MODEL = "qwen2.5:1.5b"
_DEFAULT_EMBEDDING_MODEL = "Qwen3-Embedding-8B"
_DEFAULT_RERANKER_MODEL = "Qwen3-Reranker-8B"


def coding_model() -> str:
    return os.getenv("REALMS_CODING_MODEL", _DEFAULT_CODING_MODEL)


def general_model() -> str:
    return os.getenv("REALMS_GENERAL_MODEL", _DEFAULT_GENERAL_MODEL)


def reasoning_model() -> str:
    return os.getenv("REALMS_REASONING_MODEL", _DEFAULT_REASONING_MODEL)


def local_coding_model() -> str:
    return os.getenv("OLLAMA_CODING_MODEL", _DEFAULT_LOCAL_CODING_MODEL)


def router_model() -> str:
    return os.getenv("OLLAMA_ROUTER_MODEL", _DEFAULT_ROUTER_MODEL)


def embedding_model() -> str:
    return os.getenv("REALMS_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL)


def reranker_model() -> str:
    return os.getenv("REALMS_RERANKER_MODEL", _DEFAULT_RERANKER_MODEL)


def reviewer_model(task_type: str | None) -> str:
    """Return the primary model assigned to the reviewer for a task type."""
    if task_type == "coding":
        return coding_model()
    if task_type in {"ops", "search"}:
        return general_model()
    return reasoning_model()


def model_role_label(role: str, model: str | None) -> str:
    """Render a role with explicit model provenance for human-facing output."""
    return f"{role} ({model or 'model unavailable'})"
