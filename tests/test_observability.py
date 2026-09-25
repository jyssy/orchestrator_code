import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

import mcp_server
from orchestrator import judge, model_gateway, pipeline, rag
from orchestrator.observability import (
    CallbackTraceObserver,
    TraceEventV1,
    TraceSession,
    trace_run,
)
from orchestrator.results import ComponentResult, ResultStatus


def _recording_observer(events):
    return CallbackTraceObserver(events.append)


def _patch_successful_pipeline(monkeypatch):
    monkeypatch.setattr(pipeline, "guard_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "classify_result",
        lambda prompt: ComponentResult(
            "router", ResultStatus.SUCCESS, "coding", model="router-model"
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "retrieve_context_result",
        lambda *args, **kwargs: ComponentResult(
            "retrieval", ResultStatus.SUCCESS, "", code="rag_no_matches"
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "code_result",
        lambda *args, **kwargs: ComponentResult(
            "specialist", ResultStatus.SUCCESS, "answer", model="reviewer-model"
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "critique_and_revise_result",
        lambda *args, **kwargs: ComponentResult(
            "judge", ResultStatus.SUCCESS, "answer", model="judge-model"
        ),
    )


def test_observed_run_preserves_result_schema_and_orders_metadata(monkeypatch):
    _patch_successful_pipeline(monkeypatch)
    expected = pipeline.run("synthetic request", judge_enabled=False)
    events = []

    actual = pipeline.run(
        "synthetic request",
        judge_enabled=False,
        observer=_recording_observer(events),
    )

    assert actual == expected
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.run_id for event in events}) == 1
    assert events[0].event_type == "run.started"
    assert events[-1].event_type == "run.completed"
    assert {event.event_type for event in events} >= {
        "router.completed",
        "retrieval.completed",
        "specialist.completed",
    }
    assert all(event.contract_version == 1 for event in events)
    assert all(datetime.fromisoformat(event.timestamp) for event in events)
    assert set(actual) == {
        "status",
        "task_type",
        "context_used",
        "retrieval_used",
        "repo_root",
        "policy_fingerprint",
        "model_roles",
        "draft",
        "final",
        "warnings",
        "error",
        "components",
    }


def test_observer_failure_never_changes_pipeline_result(monkeypatch):
    _patch_successful_pipeline(monkeypatch)
    expected = pipeline.run("synthetic request", judge_enabled=False)

    def fail(_event):
        raise RuntimeError("raw-observer-exception")

    actual = pipeline.run(
        "synthetic request",
        judge_enabled=False,
        observer=CallbackTraceObserver(fail),
    )

    assert actual == expected


def test_trace_contract_drops_unapproved_or_non_primitive_metadata():
    events = []
    session = TraceSession(_recording_observer(events))

    session.emit(
        "provider.attempt",
        "provider",
        "success",
        attempt=1,
        operation="completion",
        prompt="must-not-appear",
        path="/must/not/appear",
        exception=RuntimeError("must-not-appear"),
        model="configured-value-must-not-appear",
    )

    payload = events[0].to_dict()
    serialized = json.dumps(payload)
    assert payload["metadata"] == {
        "attempt": 1,
        "operation": "completion",
    }
    assert "must-not-appear" not in serialized


def test_judge_emits_critique_and_optional_revision_metadata(monkeypatch):
    responses = iter(["needs correction", "revised answer"])
    monkeypatch.setattr(judge, "reason", lambda *args, **kwargs: next(responses))
    events = []

    with trace_run(_recording_observer(events)):
        result = judge.critique_and_revise_result(
            "synthetic request", "draft", enabled=True
        )

    assert result.value == "revised answer"
    assert [event.event_type for event in events] == [
        "judge_critique.completed",
        "revision.completed",
    ]
    assert events[0].metadata["revision_required"] is True
    assert all("revised answer" not in json.dumps(event.to_dict()) for event in events)


def test_embedding_reranking_attempts_and_retry_events_are_metadata_only(
    monkeypatch,
):
    attempts = 0

    def flaky_provider():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("provider-body-must-not-appear")
        return "ok"

    monkeypatch.setenv("MODEL_REMOTE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("MODEL_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr(
        rag,
        "embedding",
        lambda **kwargs: SimpleNamespace(
            data=[{"embedding": [0.1]} for _ in kwargs["texts"]]
        ),
    )
    monkeypatch.setattr(
        rag,
        "rerank",
        lambda **kwargs: SimpleNamespace(
            json=lambda: {"results": [{"index": 0, "relevance_score": 1.0}]}
        ),
    )
    events = []

    with trace_run(_recording_observer(events)):
        assert (
            model_gateway._invoke_provider(
                flaky_provider, remote=True, operation_name="completion"
            )
            == "ok"
        )
        assert rag._embed(["synthetic text"]) == [[0.1]]
        assert rag._rerank_result("synthetic query", ["synthetic document"]).value == [
            0
        ]

    event_types = [event.event_type for event in events]
    assert event_types.count("provider.attempt") == 2
    assert "provider.retry" in event_types
    assert "embedding.completed" in event_types
    assert "reranking.completed" in event_types
    assert "provider-body-must-not-appear" not in json.dumps(
        [event.to_dict() for event in events]
    )


@pytest.mark.asyncio
async def test_observed_mcp_tool_publishes_trace_events_without_changing_result(
    monkeypatch,
):
    response = {"status": "success", "context_used": False, "retrieval_used": False}

    def fake_run(prompt, **kwargs):
        assert prompt == "synthetic request"
        kwargs["observer"].publish(
            TraceEventV1(
                run_id="00000000-0000-0000-0000-000000000000",
                sequence=1,
                timestamp="2026-09-24T00:00:00+00:00",
                event_type="run.started",
                component="pipeline",
                status="started",
            )
        )
        return response

    class FakeContext:
        def __init__(self):
            self.progress = []

        async def report_progress(self, progress, total=None, message=None):
            self.progress.append((progress, total, message))

    context = FakeContext()
    monkeypatch.setattr(mcp_server, "run", fake_run)

    result = await mcp_server.ask_orchestrator_observed(
        "synthetic request", ctx=context
    )

    assert result == response
    assert context.progress[0][0] == 1.0
    assert json.loads(context.progress[0][2])["contract_version"] == 1


@pytest.mark.asyncio
async def test_observed_mcp_progress_failure_does_not_fail_result(monkeypatch):
    response = {"status": "success", "context_used": False, "retrieval_used": False}

    def fake_run(prompt, **kwargs):
        kwargs["observer"].publish(
            TraceEventV1(
                run_id="00000000-0000-0000-0000-000000000000",
                sequence=1,
                timestamp="2026-09-24T00:00:00+00:00",
                event_type="run.started",
                component="pipeline",
                status="started",
            )
        )
        return response

    class FailingContext:
        async def report_progress(self, progress, total=None, message=None):
            raise RuntimeError("raw-progress-exception")

    monkeypatch.setattr(mcp_server, "run", fake_run)

    result = await mcp_server.ask_orchestrator_observed(
        "synthetic request", ctx=FailingContext()
    )

    assert result == response
