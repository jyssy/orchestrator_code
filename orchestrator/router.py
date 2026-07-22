"""
router.py — classifies a prompt into a task type using the local Ollama model.
Falls back to keyword heuristics if Ollama is unavailable.
"""

import os
import httpx
from typing import Literal

TaskType = Literal["coding", "ops", "general", "search"]

_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_ROUTER_MODEL = os.getenv("OLLAMA_ROUTER_MODEL", "qwen2.5:1.5b")

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


def classify(prompt: str) -> TaskType:
    """Return the task type for a given prompt."""
    try:
        response = httpx.post(
            f"{_OLLAMA_BASE}/api/generate",
            json={
                "model": _ROUTER_MODEL,
                "system": _SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0, "num_predict": 5},
            },
            timeout=10.0,
        )
        response.raise_for_status()
        raw = response.json().get("response", "").strip().lower().split()[0]
        if raw in ("coding", "ops", "search", "general"):
            return raw  # type: ignore[return-value]
        return _keyword_fallback(prompt)
    except Exception:
        # Ollama offline or model not pulled yet — use heuristics
        return _keyword_fallback(prompt)
