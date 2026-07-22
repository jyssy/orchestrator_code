"""
specialists.py — thin wrappers around REALMS models via litellm.
Each function maps to the best model for that task type.
"""

import os
import litellm
from dotenv import load_dotenv
from pathlib import Path

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
        raise RuntimeError("OFFLINE_MODE=true — skipping REALMS call")
    if not _API_KEY or _API_KEY == "your-key-here":
        raise RuntimeError(
            "REALMS_API_KEY is not set. Export it in ~/.zshrc or add it to .env"
        )
    response = litellm.completion(
        model=f"openai/{model}",
        messages=messages,
        api_base=_BASE_URL,
        api_key=_API_KEY,
        **kwargs,
    )
    return response.choices[0].message.content or ""


def _local(model: str, messages: list[dict]) -> str:
    """Call a local Ollama model via litellm. Raises with a helpful message if not running."""
    try:
        response = litellm.completion(
            model=f"ollama/{model}",
            messages=messages,
            api_base=_OLLAMA_BASE,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        raise RuntimeError(
            f"Ollama not running or model '{model}' not pulled.\n"
            f"Fix: brew install ollama && brew services start ollama && ollama pull {model}\n"
            f"Original error: {e}"
        ) from e


def code(prompt: str, context: str = "") -> str:
    """Coding specialist — Qwen3-Coder-Next (falls back to local coder)."""
    messages = []
    if context:
        messages.append({"role": "system", "content": f"Relevant context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    try:
        return _realms(_CODING_MODEL, messages)
    except RuntimeError:
        raise  # surface key/config errors directly
    except Exception as realms_err:
        # REALMS unreachable — try local fallback
        try:
            return _local(_LOCAL_CODING, messages)
        except Exception as local_err:
            raise RuntimeError(
                f"Both REALMS and local Ollama failed.\n"
                f"REALMS error: {realms_err}\n"
                f"Local error: {local_err}"
            ) from local_err


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
