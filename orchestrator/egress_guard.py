"""Single deterministic authorization and secret-scanning boundary."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from orchestrator.secret_scanner import (
    SecretFinding,
    SecretScannerError,
    SecretScannerFindings,
    scan_text,
)
from orchestrator.security import DataClassification, sensitive_content_reason


class ModelEgressBlocked(RuntimeError):
    """A model-bound payload was denied without exposing its contents."""


class RemoteTransmissionDenied(ModelEgressBlocked):
    """Repository policy allows local processing but not remote transmission."""


class ModelContentBlocked(ModelEgressBlocked):
    """Mandatory content scanning blocked the payload."""


_CLASSIFICATION: ContextVar[DataClassification] = ContextVar(
    "orchestrator_data_classification",
    default=DataClassification.LOCAL_ONLY,
)


@contextmanager
def egress_scope(classification: DataClassification) -> Iterator[None]:
    """Apply one repository classification to every model call in this request."""
    token = _CLASSIFICATION.set(classification)
    try:
        yield
    finally:
        _CLASSIFICATION.reset(token)


def current_classification() -> DataClassification:
    return _CLASSIFICATION.get()


def _classification_allows_model(*, remote: bool) -> None:
    classification = current_classification()
    if classification is DataClassification.DENY_MODEL:
        raise ModelEgressBlocked("Repository policy denies all model transmission")
    if remote and classification is not DataClassification.REMOTE_APPROVED:
        raise RemoteTransmissionDenied(
            "Repository policy does not explicitly approve remote model transmission"
        )


def guard_text(text: str, *, source: str, remote: bool | None = None) -> None:
    """Scan one model-bound text without placing its value in errors or logs."""
    if remote is not None:
        _classification_allows_model(remote=remote)

    builtin_reason = sensitive_content_reason(text)
    if builtin_reason:
        finding = SecretFinding(rule_id=f"builtin:{builtin_reason}")
        try:
            raise SecretScannerFindings((finding,))
        except SecretScannerFindings as exc:
            raise ModelContentBlocked(f"{source} was blocked; {exc}") from exc

    try:
        scan_text(text)
    except SecretScannerError as exc:
        # Scanner exceptions are deliberately metadata-only. Never include text.
        raise ModelContentBlocked(f"{source} was blocked; {exc}") from exc
    except Exception:  # noqa: BLE001 - unknown scanner failures must fail closed
        raise ModelContentBlocked(
            f"{source} was blocked; secret scanner failed unexpectedly"
        ) from None


def guard_payload(
    parts: Iterable[str],
    *,
    source: str,
    remote: bool,
) -> None:
    """Authorize and scan the fully assembled content of one model request."""
    _classification_allows_model(remote=remote)
    framed = "\n\n".join(
        f"model_payload_part_{index}:\n{part}" for index, part in enumerate(parts)
    )
    guard_text(framed, source=source)
