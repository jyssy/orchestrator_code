"""Versioned records used by the enforced plan/approval workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any

SCHEMA_VERSION = 1


def _canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _require_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_tuple(data: dict[str, Any], field: str) -> tuple[str, ...]:
    value = data.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class RepositorySnapshot:
    """Read-only Git and working-tree identity captured at planning time."""

    repo_root: str
    base_commit: str
    working_tree_fingerprint: str
    changed_paths: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepositorySnapshot:
        if not isinstance(data, dict):
            raise TypeError("repository must be an object")
        return cls(
            repo_root=_require_string(data, "repo_root"),
            base_commit=_require_string(data, "base_commit"),
            working_tree_fingerprint=_require_string(
                data, "working_tree_fingerprint"
            ),
            changed_paths=_string_tuple(data, "changed_paths"),
        )


@dataclass(frozen=True)
class PolicyIdentity:
    """Identity of the policy sources and caller constraints used for a plan."""

    fingerprint: str
    sources: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyIdentity:
        if not isinstance(data, dict):
            raise TypeError("policy must be an object")
        return cls(
            fingerprint=_require_string(data, "fingerprint"),
            sources=_string_tuple(data, "sources"),
        )


@dataclass(frozen=True)
class StructuredPlan:
    """Machine-verifiable envelope around the architect's human-readable plan."""

    schema_version: int
    plan_id: str
    task: str
    repository: RepositorySnapshot
    policy: PolicyIdentity
    effective_constraints: str
    allowed_paths: tuple[str, ...]
    prohibited_operations: tuple[str, ...]
    required_checks: tuple[str, ...]
    proposal: str

    def _payload(self, *, include_plan_id: bool) -> dict[str, Any]:
        data = asdict(self)
        if not include_plan_id:
            data.pop("plan_id")
        return data

    @property
    def digest(self) -> str:
        """Return the digest of every authoritative plan field except its ID."""
        payload = _canonical_json(self._payload(include_plan_id=False))
        return hashlib.sha256(payload.encode()).hexdigest()

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported plan schema version: {self.schema_version}")
        if self.plan_id != self.digest:
            raise ValueError("plan_id does not match the plan content")
        if not self.allowed_paths:
            raise ValueError("A structured plan must contain at least one allowed path")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(self._payload(include_plan_id=True), indent=2, sort_keys=True)

    @classmethod
    def create(
        cls,
        *,
        task: str,
        repository: RepositorySnapshot,
        policy: PolicyIdentity,
        effective_constraints: str,
        allowed_paths: tuple[str, ...],
        prohibited_operations: tuple[str, ...],
        required_checks: tuple[str, ...],
        proposal: str,
    ) -> StructuredPlan:
        plan = cls(
            schema_version=SCHEMA_VERSION,
            plan_id="pending",
            task=task,
            repository=repository,
            policy=policy,
            effective_constraints=effective_constraints,
            allowed_paths=allowed_paths,
            prohibited_operations=prohibited_operations,
            required_checks=required_checks,
            proposal=proposal,
        )
        return replace(plan, plan_id=plan.digest)

    @classmethod
    def from_json(cls, raw: str) -> StructuredPlan:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid plan JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise TypeError("Plan JSON must contain an object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported plan schema version: {version}")
        plan = cls(
            schema_version=version,
            plan_id=_require_string(data, "plan_id"),
            task=_require_string(data, "task"),
            repository=RepositorySnapshot.from_dict(data.get("repository")),
            policy=PolicyIdentity.from_dict(data.get("policy")),
            effective_constraints=data.get("effective_constraints", ""),
            allowed_paths=_string_tuple(data, "allowed_paths"),
            prohibited_operations=_string_tuple(data, "prohibited_operations"),
            required_checks=_string_tuple(data, "required_checks"),
            proposal=_require_string(data, "proposal"),
        )
        if not isinstance(plan.effective_constraints, str):
            raise TypeError("effective_constraints must be a string")
        plan.validate()
        return plan


@dataclass(frozen=True)
class ApprovalRecord:
    """Single-use human approval bound to an exact structured plan."""

    schema_version: int
    approval_id: str
    record_digest: str
    plan_id: str
    plan_digest: str
    task: str
    repo_root: str
    base_commit: str
    working_tree_fingerprint: str
    policy_fingerprint: str
    approved_by: str
    approved_at: str
    consumed_at: str | None = None

    def _payload(self, *, include_approval_id: bool) -> dict[str, Any]:
        data = asdict(self)
        if not include_approval_id:
            data.pop("approval_id")
        return data

    @property
    def digest(self) -> str:
        payload_data = self._payload(include_approval_id=False)
        payload_data.pop("consumed_at")
        payload_data.pop("record_digest")
        payload = _canonical_json(payload_data)
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def integrity_digest(self) -> str:
        payload_data = self._payload(include_approval_id=True)
        payload_data.pop("record_digest")
        payload = _canonical_json(payload_data)
        return hashlib.sha256(payload.encode()).hexdigest()

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported approval schema version: {self.schema_version}"
            )
        if self.approval_id != self.digest:
            raise ValueError("approval_id does not match the approval content")
        if self.record_digest != self.integrity_digest:
            raise ValueError("record_digest does not match the approval state")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(self._payload(include_approval_id=True), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> ApprovalRecord:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid approval JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise TypeError("Approval JSON must contain an object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported approval schema version: {version}")
        consumed_at = data.get("consumed_at")
        if consumed_at is not None and not isinstance(consumed_at, str):
            raise ValueError("consumed_at must be a string or null")
        record = cls(
            schema_version=version,
            approval_id=_require_string(data, "approval_id"),
            record_digest=_require_string(data, "record_digest"),
            plan_id=_require_string(data, "plan_id"),
            plan_digest=_require_string(data, "plan_digest"),
            task=_require_string(data, "task"),
            repo_root=_require_string(data, "repo_root"),
            base_commit=_require_string(data, "base_commit"),
            working_tree_fingerprint=_require_string(
                data, "working_tree_fingerprint"
            ),
            policy_fingerprint=_require_string(data, "policy_fingerprint"),
            approved_by=_require_string(data, "approved_by"),
            approved_at=_require_string(data, "approved_at"),
            consumed_at=consumed_at,
        )
        record.validate()
        return record
