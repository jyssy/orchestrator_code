"""Opt-in, metadata-only tracing for orchestration runs."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Protocol

TRACE_CONTRACT_VERSION = 1

_ALLOWED_EVENT_TYPES = frozenset(
    {
        "run.started",
        "router.completed",
        "retrieval.completed",
        "embedding.completed",
        "reranking.completed",
        "specialist.completed",
        "judge_critique.completed",
        "revision.completed",
        "revision.skipped",
        "provider.attempt",
        "provider.retry",
        "run.completed",
    }
)
_ALLOWED_COMPONENTS = frozenset(
    {
        "pipeline",
        "router",
        "retrieval",
        "embedding",
        "reranker",
        "specialist",
        "judge",
        "revision",
        "provider",
    }
)
_ALLOWED_STATUSES = frozenset(
    {"started", "success", "degraded", "failed", "skipped", "retrying"}
)
_ALLOWED_METADATA_KEYS = frozenset(
    {
        "attempt",
        "attempts",
        "batch_size",
        "candidate_count",
        "code",
        "context_used",
        "fallback",
        "judge_enabled",
        "max_attempts",
        "operation",
        "provider",
        "remote",
        "result_status",
        "retrieval_used",
        "revision_required",
        "selected_count",
        "task_type",
    }
)
_BOOLEAN_METADATA_KEYS = frozenset(
    {
        "context_used",
        "fallback",
        "judge_enabled",
        "remote",
        "retrieval_used",
        "revision_required",
    }
)
_INTEGER_METADATA_KEYS = frozenset(
    {
        "attempt",
        "attempts",
        "batch_size",
        "candidate_count",
        "max_attempts",
        "selected_count",
    }
)
_ENUM_METADATA_VALUES = {
    "operation": frozenset({"completion", "embedding", "reranking", "routing"}),
    "provider": frozenset({"local", "remote"}),
    "result_status": frozenset(
        {
            "success",
            "degraded_success",
            "unavailable_dependency",
            "invalid_configuration",
            "invalid_input",
            "security_block",
            "internal_failure",
        }
    ),
    "task_type": frozenset({"coding", "general", "ops", "search"}),
}
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}").fullmatch


@dataclass(frozen=True)
class TraceEventV1:
    """Version 1 of the transport-neutral, metadata-only trace contract."""

    run_id: str
    sequence: int
    timestamp: str
    event_type: str
    component: str
    status: str
    duration_ms: int | None = None
    metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    contract_version: int = TRACE_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TraceObserver(Protocol):
    """A non-blocking destination for trace events."""

    def publish(self, event: TraceEventV1) -> None: ...


class NullTraceObserver:
    """Default observer; intentionally performs no work."""

    def publish(self, event: TraceEventV1) -> None:
        del event


@dataclass(frozen=True)
class CallbackTraceObserver:
    """Adapt a non-blocking callback to the observer protocol."""

    callback: Callable[[TraceEventV1], None]

    def publish(self, event: TraceEventV1) -> None:
        self.callback(event)


_NULL_OBSERVER = NullTraceObserver()


def _safe_metadata(
    metadata: Mapping[str, object],
) -> dict[str, str | int | float | bool]:
    """Accept only explicitly approved primitive metadata fields."""
    safe: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if key not in _ALLOWED_METADATA_KEYS:
            continue
        valid_boolean = key in _BOOLEAN_METADATA_KEYS and isinstance(value, bool)
        valid_integer = (
            key in _INTEGER_METADATA_KEYS
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        )
        valid_enum = (
            key in _ENUM_METADATA_VALUES
            and isinstance(value, str)
            and value in _ENUM_METADATA_VALUES[key]
        )
        valid_code = key == "code" and isinstance(value, str) and _SAFE_CODE(value)
        if valid_boolean or valid_integer or valid_enum or valid_code:
            safe[key] = value
    return safe


def duration_ms(started: float) -> int:
    """Return a non-negative monotonic elapsed duration in milliseconds."""
    return max(0, round((time.perf_counter() - started) * 1000))


class TraceSession:
    """Own run identity and monotonic event ordering for one request."""

    def __init__(self, observer: TraceObserver | None = None) -> None:
        self.run_id = str(uuid.uuid4())
        self._observer = observer or _NULL_OBSERVER
        self._sequence = 0

    def emit(
        self,
        event_type: str,
        component: str,
        status: str,
        *,
        duration_ms: int | None = None,
        **metadata: object,
    ) -> None:
        """Publish one validated event; observer failures are always ignored."""
        if (
            event_type not in _ALLOWED_EVENT_TYPES
            or component not in _ALLOWED_COMPONENTS
            or status not in _ALLOWED_STATUSES
        ):
            return
        self._sequence += 1
        event = TraceEventV1(
            run_id=self.run_id,
            sequence=self._sequence,
            timestamp=datetime.now(UTC).isoformat(),
            event_type=event_type,
            component=component,
            status=status,
            duration_ms=(max(0, duration_ms) if duration_ms is not None else None),
            metadata=_safe_metadata(metadata),
        )
        try:
            self._observer.publish(event)
        except Exception:  # noqa: BLE001 - observation cannot affect orchestration
            return


_CURRENT_TRACE: ContextVar[TraceSession | None] = ContextVar(
    "orchestrator_trace_session", default=None
)


@contextmanager
def trace_run(observer: TraceObserver | None = None) -> Iterator[TraceSession]:
    """Bind a trace session for the current orchestration call."""
    session = TraceSession(observer)
    token = _CURRENT_TRACE.set(session)
    try:
        yield session
    finally:
        _CURRENT_TRACE.reset(token)


def emit_trace(
    event_type: str,
    component: str,
    status: str,
    *,
    elapsed_from: float | None = None,
    **metadata: object,
) -> None:
    """Emit through the active session, or do nothing outside an observed run."""
    session = _CURRENT_TRACE.get()
    if session is None:
        return
    session.emit(
        event_type,
        component,
        status,
        duration_ms=duration_ms(elapsed_from) if elapsed_from is not None else None,
        **metadata,
    )


def trace_status(value: object) -> str:
    """Map internal result status values to the small public trace vocabulary."""
    raw = getattr(value, "value", value)
    if raw == "success":
        return "success"
    if raw == "degraded_success":
        return "degraded"
    return "failed"
