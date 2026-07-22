"""
specialists.py — thin wrappers around REALMS models via litellm.
Each function maps to the best model for that task type.
"""

import os
import litellm

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
    """Call a REALMS model. Raises if offline mode is set."""
    if _OFFLINE:
        raise RuntimeError("OFFLINE_MODE=true — skipping REALMS call")
    response = litellm.completion(
        model=f"openai/{model}",
        messages=messages,
        api_base=_BASE_URL,
        api_key=_API_KEY,
        **kwargs,
    )
    return response.choices[0].message.content or ""


def _local(model: str, messages: list[dict]) -> str:
    """Call a local Ollama model via litellm."""
    response = litellm.completion(
        model=f"ollama/{model}",
        messages=messages,
        api_base=_OLLAMA_BASE,
    )
    return response.choices[0].message.content or ""


def code(prompt: str, context: str = "") -> str:
    """Coding specialist — Qwen3-Coder-Next (falls back to local coder)."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    try:
        return _realms(_CODING_MODEL, messages)
    except Exception:
        return _local(_LOCAL_CODING, messages)


def ops(prompt: str, context: str = "") -> str:
    """Ops/infra specialist — gemma-4-31B-it (large context for big playbooks)."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    return _realms(_GENERAL_MODEL, messages)


def reason(prompt: str, context: str = "") -> str:
    """Heavy reasoning — gpt-oss-120b."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    return _realms(_REASONING_MODEL, messages)


def summarize(prompt: str, context: str = "") -> str:
    """General summarization/explanation — gemma-4-31B-it."""
    return ops(prompt, context)
