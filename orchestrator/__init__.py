"""Public API with lazy imports so lightweight CLI commands start quickly."""

__all__ = ["classify", "code", "reason", "summarize", "retrieve_context", "critique_and_revise"]


def __getattr__(name: str):
    if name == "classify":
        from orchestrator.router import classify

        return classify
    if name in {"code", "reason", "summarize"}:
        from orchestrator import specialists

        return getattr(specialists, name)
    if name == "retrieve_context":
        from orchestrator.rag import retrieve_context

        return retrieve_context
    if name == "critique_and_revise":
        from orchestrator.judge import critique_and_revise

        return critique_and_revise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
