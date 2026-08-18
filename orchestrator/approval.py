"""Approval persistence, drift detection, and changed-path validation."""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from orchestrator.models import SCHEMA_VERSION, ApprovalRecord, StructuredPlan

DEFAULT_PROHIBITED_OPERATIONS = (
    "commit",
    "push",
    "merge",
    "tag",
    "release",
    "deploy",
    "access secrets",
    "infrastructure plans or playbooks",
    "migrations",
    "service restarts",
)


def state_root() -> Path:
    """Return the user-local state root; runtime artifacts stay outside repositories."""
    configured = os.getenv("ORCHESTRATOR_STATE_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".orchestrator" / "workflow"
    ).resolve()


def _write_private(path: Path, content: str) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        path.parent.chmod(0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _require_outside_repository(path: Path, repo_root: str) -> None:
    destination = path.expanduser().resolve()
    root = Path(repo_root).expanduser().resolve()
    try:
        destination.relative_to(root)
    except ValueError:
        return
    raise ValueError("Plan and approval records must be stored outside the target repository")


def save_plan(plan: StructuredPlan, directory: Path | None = None) -> Path:
    destination = (directory or state_root() / "plans") / f"{plan.plan_id}.json"
    _require_outside_repository(destination, plan.repository.repo_root)
    _write_private(destination, plan.to_json())
    return destination


def load_plan(path: str | Path) -> StructuredPlan:
    return StructuredPlan.from_json(Path(path).expanduser().read_text(encoding="utf-8"))


def find_latest_plan(repo_root: Path, *, directory: Path | None = None) -> Path:
    """Return the most recently written plan record for a repository.

    Scoped to one repository so ``--latest`` cannot pick up a plan created for
    a different repo just because it happens to be newer.
    """
    plans_dir = directory or state_root() / "plans"
    target = str(repo_root.expanduser().resolve())
    candidates: list[tuple[float, Path]] = []
    for path in plans_dir.glob("*.json"):
        try:
            plan = load_plan(path)
        except (OSError, ValueError):
            continue
        if plan.repository.repo_root == target:
            candidates.append((path.stat().st_mtime, path))
    if not candidates:
        raise ValueError(f"No plan record found for repository: {target}")
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def find_latest_unconsumed_approval(repo_root: Path, *, directory: Path | None = None) -> Path:
    """Return the most recently approved, not-yet-consumed approval for a repository."""
    approvals_dir = directory or state_root() / "approvals"
    target = str(repo_root.expanduser().resolve())
    candidates: list[tuple[str, Path]] = []
    for path in approvals_dir.glob("*.json"):
        try:
            approval = load_approval(path)
        except (OSError, ValueError):
            continue
        if approval.repo_root == target and not approval.consumed_at:
            candidates.append((approval.approved_at, path))
    if not candidates:
        raise ValueError(f"No unconsumed approval found for repository: {target}")
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def resolve_latest_execution_records(
    repo_root: Path,
    *,
    plans_directory: Path | None = None,
    approvals_directory: Path | None = None,
) -> tuple[Path, Path]:
    """Return (plan_path, approval_path) for the repo's latest unconsumed approval."""
    approval_path = find_latest_unconsumed_approval(repo_root, directory=approvals_directory)
    approval = load_approval(approval_path)
    plan_path = (plans_directory or state_root() / "plans") / f"{approval.plan_id}.json"
    if not plan_path.is_file():
        raise ValueError(f"Plan record for latest approval is missing: {plan_path}")
    return plan_path, approval_path


def create_approval(plan: StructuredPlan, approved_by: str) -> ApprovalRecord:
    approver = approved_by.strip()
    if not approver:
        raise ValueError("approved_by must not be empty")
    record = ApprovalRecord(
        schema_version=SCHEMA_VERSION,
        approval_id="pending",
        record_digest="pending",
        plan_id=plan.plan_id,
        plan_digest=plan.digest,
        task=plan.task,
        repo_root=plan.repository.repo_root,
        base_commit=plan.repository.base_commit,
        working_tree_fingerprint=plan.repository.working_tree_fingerprint,
        policy_fingerprint=plan.policy.fingerprint,
        approved_by=approver,
        approved_at=datetime.now(UTC).isoformat(),
    )
    record = replace(record, approval_id=record.digest)
    return replace(record, record_digest=record.integrity_digest)


def validate_plan_state(
    plan: StructuredPlan,
    *,
    current_base_commit: str,
    current_working_tree_fingerprint: str,
    current_policy_fingerprint: str,
) -> None:
    """Require the plan to still describe current repository and policy state."""
    mismatches = []
    if current_base_commit != plan.repository.base_commit:
        mismatches.append("base commit")
    if current_working_tree_fingerprint != plan.repository.working_tree_fingerprint:
        mismatches.append("working tree")
    if current_policy_fingerprint != plan.policy.fingerprint:
        mismatches.append("effective policy")
    if mismatches:
        raise ValueError("Plan is stale due to drift: " + ", ".join(mismatches))


def save_approval(
    approval: ApprovalRecord, directory: Path | None = None
) -> Path:
    destination = (
        directory or state_root() / "approvals"
    ) / f"{approval.approval_id}.json"
    _require_outside_repository(destination, approval.repo_root)
    _write_private(destination, approval.to_json())
    return destination


def load_approval(path: str | Path) -> ApprovalRecord:
    return ApprovalRecord.from_json(
        Path(path).expanduser().read_text(encoding="utf-8")
    )


def validate_approval_location(path: str | Path, approval: ApprovalRecord) -> None:
    """Require CLI execution to consume the canonical user-local approval file."""
    actual = Path(path).expanduser().resolve()
    expected = (
        state_root() / "approvals" / f"{approval.approval_id}.json"
    ).resolve()
    if actual != expected:
        raise ValueError(
            "Execution requires the canonical approval record created by approve: "
            f"{expected}"
        )


def validate_approval(
    plan: StructuredPlan,
    approval: ApprovalRecord,
    *,
    current_base_commit: str,
    current_working_tree_fingerprint: str,
    current_policy_fingerprint: str,
) -> None:
    """Raise when approval does not authorize this plan in the current state."""
    approval.validate()
    plan.validate()
    validate_plan_state(
        plan,
        current_base_commit=current_base_commit,
        current_working_tree_fingerprint=current_working_tree_fingerprint,
        current_policy_fingerprint=current_policy_fingerprint,
    )
    checks = {
        "plan ID": (approval.plan_id, plan.plan_id),
        "plan digest": (approval.plan_digest, plan.digest),
        "task": (approval.task, plan.task),
        "repository root": (approval.repo_root, plan.repository.repo_root),
        "approved base commit": (approval.base_commit, plan.repository.base_commit),
        "approved working tree": (
            approval.working_tree_fingerprint,
            plan.repository.working_tree_fingerprint,
        ),
        "approved policy": (approval.policy_fingerprint, plan.policy.fingerprint),
    }
    mismatches = [label for label, (actual, expected) in checks.items() if actual != expected]
    if mismatches:
        raise ValueError("Approval is invalid due to drift or mismatch: " + ", ".join(mismatches))
    if approval.consumed_at:
        raise ValueError("Approval has already been consumed")


def consume_approval(path: str | Path, approval: ApprovalRecord) -> ApprovalRecord:
    if approval.consumed_at:
        raise ValueError("Approval has already been consumed")
    consumed = replace(
        approval,
        record_digest="pending",
        consumed_at=datetime.now(UTC).isoformat(),
    )
    consumed = replace(consumed, record_digest=consumed.integrity_digest)
    destination = Path(path).expanduser()
    _require_outside_repository(destination, approval.repo_root)
    marker = destination.with_name(f"{destination.name}.consumed")
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("Approval has already been consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{approval.approval_id}\n{consumed.consumed_at}\n")
    _write_private(destination, consumed.to_json())
    return consumed


def normalize_allowed_paths(patterns: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().replace("\\", "/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        path = PurePosixPath(pattern)
        has_drive = len(pattern) >= 2 and pattern[1] == ":"
        if (
            not pattern
            or "\0" in pattern
            or path.is_absolute()
            or has_drive
            or ".." in path.parts
        ):
            raise ValueError(f"Allowed path must be repository-relative: {raw_pattern}")
        if path.parts[0] == ".git":
            raise ValueError("The .git directory cannot be included in allowed paths")
        if pattern.endswith("/"):
            pattern += "**"
        if pattern not in normalized:
            normalized.append(pattern)
    if not normalized:
        raise ValueError("At least one allowed path is required")
    return tuple(normalized)


def path_is_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    candidate = path.replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    pure = PurePosixPath(candidate)
    if not candidate or pure.is_absolute() or ".." in pure.parts:
        return False
    for pattern in allowed_paths:
        prefix = pattern.removesuffix("/**")
        if prefix != pattern and (candidate == prefix or candidate.startswith(f"{prefix}/")):
            return True
        if pure.match(pattern):
            return True
    return False


def validate_changed_paths(
    changed_paths: set[str] | tuple[str, ...], allowed_paths: tuple[str, ...]
) -> None:
    disallowed = sorted(
        path for path in changed_paths if not path_is_allowed(path, allowed_paths)
    )
    if disallowed:
        raise ValueError("Changed paths exceed the approved scope: " + ", ".join(disallowed))
