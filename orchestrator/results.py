"""Typed, content-safe component and pipeline result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class ResultStatus(str, Enum):
    SUCCESS = "success"
    DEGRADED_SUCCESS = "degraded_success"
    UNAVAILABLE_DEPENDENCY = "unavailable_dependency"
    INVALID_INPUT = "invalid_input"
    INVALID_CONFIGURATION = "invalid_configuration"
    SECURITY_BLOCK = "security_block"
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True)
class Diagnostic:
    """A fixed, content-free warning safe for CLI and MCP responses."""

    component: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ComponentResult(Generic[T]):
    """A component value plus its public operational status."""

    component: str
    status: ResultStatus
    value: T | None = None
    code: str = "ok"
    message: str = "Completed successfully."
    attempts: int = 1
    warnings: tuple[Diagnostic, ...] = field(default_factory=tuple)
    model: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in {
            ResultStatus.SUCCESS,
            ResultStatus.DEGRADED_SUCCESS,
        }

    def public_summary(self) -> dict[str, str | int | None]:
        return {
            "component": self.component,
            "model": self.model,
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "attempts": self.attempts,
        }


_STATUS_SEVERITY = {
    ResultStatus.SUCCESS: 0,
    ResultStatus.DEGRADED_SUCCESS: 1,
    ResultStatus.UNAVAILABLE_DEPENDENCY: 2,
    ResultStatus.INVALID_INPUT: 3,
    ResultStatus.INVALID_CONFIGURATION: 4,
    ResultStatus.INTERNAL_FAILURE: 5,
    ResultStatus.SECURITY_BLOCK: 6,
}


def overall_status(results: list[ComponentResult[object]]) -> ResultStatus:
    """Return the most severe component status deterministically."""
    if not results:
        return ResultStatus.SUCCESS
    return max(results, key=lambda result: _STATUS_SEVERITY[result.status]).status


def diagnostic(component: str, code: str, message: str) -> Diagnostic:
    """Construct diagnostics only from developer-authored constant strings."""
    return Diagnostic(component=component, code=code, message=message)
