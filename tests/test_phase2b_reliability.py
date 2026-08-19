import json

import httpx
import pytest

import mcp_server
from orchestrator import judge, model_gateway, pipeline, rag, router, specialists
from orchestrator.egress_guard import ModelContentBlocked, RemoteTransmissionDenied
from orchestrator.model_gateway import ProviderFailure
from orchestrator.results import ComponentResult, ResultStatus


def _messages():
    return [{"role": "user", "content": "ordinary request"}]


def test_configured_coding_model_is_used_and_reported(monkeypatch):
    received = {}
    monkeypatch.setenv("REALMS_CODING_MODEL", "replacement-coder")

    def fake_realms(model, messages, **kwargs):
        received["model"] = model
        return "answer"

    monkeypatch.setattr(specialists, "_realms", fake_realms)

    result = specialists.code_result("prompt")

    assert received["model"] == "replacement-coder"
    assert result.model == "replacement-coder"


def test_local_coding_fallback_is_reported_as_the_used_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_CODING_MODEL", "replacement-local-coder")
    monkeypatch.setattr(
        specialists,
        "_realms",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RemoteTransmissionDenied("local-only")
        ),
    )
    monkeypatch.setattr(specialists, "_local", lambda *args, **kwargs: "answer")

    result = specialists.code_result("prompt")

    assert result.model == "replacement-local-coder"


def test_remote_gateway_retries_only_transient_failures(monkeypatch):
    attempts = []
    sentinel = "provider-body-must-not-escape"

    class Response:
        pass

    def flaky(**kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise httpx.ConnectTimeout(sentinel)
        return Response()

    monkeypatch.setenv("MODEL_REMOTE_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MODEL_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr(model_gateway, "guard_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_gateway.litellm, "completion", flaky)

    result = model_gateway.completion(
        model="openai/example",
        messages=_messages(),
        api_base="https://models.invalid/v1",
        api_key="credential",
        remote=True,
    )

    assert result.__class__ is Response
    assert len(attempts) == 3


def test_remote_gateway_exhaustion_is_bounded_and_redacted(monkeypatch):
    attempts = 0
    sentinel = "provider-body-must-not-escape"

    def unavailable(**kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout(sentinel)

    monkeypatch.setenv("MODEL_REMOTE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("MODEL_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr(model_gateway, "guard_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_gateway.litellm, "completion", unavailable)

    with pytest.raises(ProviderFailure) as captured:
        model_gateway.completion(
            model="openai/example",
            messages=_messages(),
            api_base="https://models.invalid/v1",
            remote=True,
        )

    assert attempts == 2
    assert captured.value.status is ResultStatus.UNAVAILABLE_DEPENDENCY
    assert captured.value.attempts == 2
    assert sentinel not in str(captured.value)


def test_invalid_retry_configuration_never_calls_provider(monkeypatch):
    attempts = 0

    def provider(**kwargs):
        nonlocal attempts
        attempts += 1

    monkeypatch.setenv("MODEL_REMOTE_MAX_ATTEMPTS", "many")
    monkeypatch.setattr(model_gateway, "guard_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_gateway.litellm, "completion", provider)

    with pytest.raises(ProviderFailure) as captured:
        model_gateway.completion(
            model="openai/example",
            messages=_messages(),
            api_base="https://models.invalid/v1",
            remote=True,
        )

    assert attempts == 0
    assert captured.value.status is ResultStatus.INVALID_CONFIGURATION
    assert captured.value.code == "invalid_retry_configuration"
    assert captured.value.__cause__ is None


def test_authentication_and_security_failures_are_never_retried(monkeypatch):
    attempts = 0

    class AuthenticationFailure(Exception):
        status_code = 401

    def rejected(**kwargs):
        nonlocal attempts
        attempts += 1
        raise AuthenticationFailure("credential-value")

    monkeypatch.setenv("MODEL_REMOTE_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("MODEL_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr(model_gateway, "guard_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_gateway.litellm, "completion", rejected)

    with pytest.raises(ProviderFailure) as captured:
        model_gateway.completion(
            model="openai/example",
            messages=_messages(),
            api_base="https://models.invalid/v1",
            remote=True,
        )

    assert attempts == 1
    assert captured.value.status is ResultStatus.INVALID_CONFIGURATION
    assert "credential-value" not in str(captured.value)

    monkeypatch.setattr(
        model_gateway,
        "guard_payload",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ModelContentBlocked("redacted scanner failure")
        ),
    )
    with pytest.raises(ModelContentBlocked):
        model_gateway.completion(
            model="openai/example",
            messages=_messages(),
            api_base="https://models.invalid/v1",
            remote=True,
        )
    assert attempts == 1


def test_router_fallback_is_explicit(monkeypatch):
    monkeypatch.setattr(
        router,
        "ollama_generate",
        lambda **kwargs: (_ for _ in ()).throw(
            ProviderFailure(
                ResultStatus.UNAVAILABLE_DEPENDENCY,
                "provider_temporarily_unavailable",
                "The model provider is temporarily unavailable.",
            )
        ),
    )

    result = router.classify_result("write a Python function")

    assert result.status is ResultStatus.DEGRADED_SUCCESS
    assert result.value == "coding"
    assert result.code == "router_heuristic_fallback"


def test_missing_index_and_no_matches_are_distinct(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(rag, "_INDEX_PATH", missing)
    result = rag.retrieve_context_result("query")
    assert result.status is ResultStatus.UNAVAILABLE_DEPENDENCY
    assert result.code == "rag_index_missing"

    index_path = tmp_path / "empty-index"
    client = rag.chromadb.PersistentClient(path=str(index_path))
    client.get_or_create_collection(rag._COLLECTION_NAME)
    monkeypatch.setattr(rag, "_INDEX_PATH", index_path)
    result = rag.retrieve_context_result("query")
    assert result.status is ResultStatus.SUCCESS
    assert result.code == "rag_no_matches"


def test_planner_and_judge_prompts_instruct_against_fabrication():
    assert "is not present in the provided context" in pipeline._PLAN_SYSTEM
    assert "flag that as a likely fabrication" in judge._CRITIQUE_SYSTEM
    assert (
        "the information is not present in the provided context"
        in judge._REVISE_SYSTEM
    )


def test_reranker_fallback_is_visible(monkeypatch):
    monkeypatch.setattr(
        rag,
        "rerank",
        lambda **kwargs: (_ for _ in ()).throw(
            ProviderFailure(
                ResultStatus.UNAVAILABLE_DEPENDENCY,
                "provider_temporarily_unavailable",
                "The model provider is temporarily unavailable.",
                attempts=3,
            )
        ),
    )

    result = rag._rerank_result("query", ["one", "two"])

    assert result.status is ResultStatus.DEGRADED_SUCCESS
    assert result.value == [0, 1]
    assert result.attempts == 3
    assert result.code == "reranker_unavailable_fallback"


def test_malformed_reranker_indices_use_visible_stable_fallback(monkeypatch):
    class Response:
        @staticmethod
        def json():
            return {"results": [{"index": 99, "relevance_score": 1.0}]}

    monkeypatch.setattr(rag, "rerank", lambda **kwargs: Response())

    result = rag._rerank_result("query", ["one", "two"])

    assert result.status is ResultStatus.DEGRADED_SUCCESS
    assert result.value == [0, 1]
    assert result.code == "reranker_malformed_response"


def test_judge_fallback_preserves_safe_failure_category(monkeypatch):
    sentinel = "provider-detail-must-not-escape"
    monkeypatch.setattr(
        judge,
        "reason",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ProviderFailure(
                ResultStatus.INVALID_CONFIGURATION,
                "provider_authentication_failed",
                sentinel,
            )
        ),
    )

    result = judge.critique_and_revise_result("prompt", "draft", enabled=True)

    assert result.status is ResultStatus.DEGRADED_SUCCESS
    assert result.value == "draft"
    assert result.code == "judge_invalid_configuration"
    assert sentinel not in json.dumps(result.public_summary())


def _patch_pipeline_success(monkeypatch, retrieval_result):
    monkeypatch.setattr(pipeline, "guard_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "classify_result",
        lambda prompt: ComponentResult("router", ResultStatus.SUCCESS, "coding"),
    )
    monkeypatch.setattr(
        pipeline,
        "retrieve_context_result",
        lambda *args, **kwargs: retrieval_result,
    )
    monkeypatch.setattr(
        pipeline,
        "code_result",
        lambda *args, **kwargs: ComponentResult(
            "specialist",
            ResultStatus.SUCCESS,
            "answer",
            model="Qwen3-Coder-Next",
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "critique_and_revise_result",
        lambda *args, **kwargs: ComponentResult(
            "judge", ResultStatus.SUCCESS, "answer", model="gpt-oss-120b"
        ),
    )


def test_pipeline_returns_answer_with_visible_retrieval_degradation(monkeypatch):
    _patch_pipeline_success(
        monkeypatch,
        ComponentResult(
            "retrieval",
            ResultStatus.UNAVAILABLE_DEPENDENCY,
            code="rag_index_missing",
            message="The local RAG index is unavailable.",
        ),
    )

    result = pipeline.run("ordinary request", judge_enabled=False)

    assert result["status"] == "degraded_success"
    assert result["final"] == "answer"
    assert result["model_roles"] == {
        "reviewer": "Qwen3-Coder-Next",
        "judge": "gpt-oss-120b",
    }
    assert next(
        component
        for component in result["components"]
        if component["component"] == "specialist"
    )["model"] == "Qwen3-Coder-Next"
    assert result["retrieval_used"] is False
    assert result["warnings"][0]["code"] == "rag_index_missing"


def test_pipeline_failure_and_internal_errors_are_content_safe(monkeypatch):
    _patch_pipeline_success(
        monkeypatch,
        ComponentResult("retrieval", ResultStatus.SUCCESS, ""),
    )
    monkeypatch.setattr(
        pipeline,
        "code_result",
        lambda *args, **kwargs: ComponentResult(
            "specialist",
            ResultStatus.INVALID_CONFIGURATION,
            code="remote_provider_key_missing",
            message="Remote model authentication is not configured.",
        ),
    )
    result = pipeline.run("ordinary request", judge_enabled=False)
    assert result["status"] == "invalid_configuration"
    assert result["final"] == ""
    assert result["error"]["code"] == "remote_provider_key_missing"

    sentinel = "exception-secret-value"
    monkeypatch.setattr(
        pipeline,
        "classify_result",
        lambda prompt: (_ for _ in ()).throw(RuntimeError(sentinel)),
    )
    result = pipeline.run("ordinary request", judge_enabled=False)
    assert result["status"] == "internal_failure"
    assert sentinel not in json.dumps(result)


def test_pipeline_security_block_has_no_answer_or_payload(monkeypatch):
    sentinel = "blocked-secret-value"
    monkeypatch.setattr(
        pipeline,
        "guard_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(ModelContentBlocked(sentinel)),
    )

    result = pipeline.run("ordinary request", judge_enabled=False)

    assert result["status"] == "security_block"
    assert result["final"] == ""
    assert sentinel not in json.dumps(result)


def test_empty_prompt_is_an_explicit_invalid_input():
    result = pipeline.run("   ")

    assert result["status"] == "invalid_input"
    assert result["error"]["component"] == "pipeline"
    assert result["error"]["code"] == "empty_prompt"


def test_mcp_text_and_structured_responses_include_model_attribution(monkeypatch):
    response = {
        "status": "degraded_success",
        "final": "answer text",
        "model_roles": {
            "reviewer": "Qwen3-Coder-Next",
            "judge": "gpt-oss-120b",
        },
        "warnings": [
            {
                "component": "retrieval",
                "code": "rag_index_missing",
                "message": "The local RAG index is unavailable.",
            }
        ],
    }
    monkeypatch.setattr(mcp_server, "run", lambda *args, **kwargs: response)

    assert mcp_server.ask_orchestrator("prompt") == (
        "Reviewer (Qwen3-Coder-Next) | Judge (gpt-oss-120b)\n\nanswer text"
    )
    assert mcp_server.ask_orchestrator_structured("prompt") == response
