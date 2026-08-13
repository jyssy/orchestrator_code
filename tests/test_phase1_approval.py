import json
import subprocess
from dataclasses import replace

import pytest

from orchestrator import pipeline
from orchestrator.approval import (
    consume_approval,
    create_approval,
    load_approval,
    normalize_allowed_paths,
    path_is_allowed,
    save_approval,
    validate_approval,
    validate_approval_location,
    validate_changed_paths,
    validate_plan_state,
)
from orchestrator.context import (
    capture_repository_snapshot,
    load_policy_identity,
    reload_policy_identity,
)
from orchestrator.models import PolicyIdentity, RepositorySnapshot, StructuredPlan


def make_plan(tmp_path, *, allowed_paths=("orchestrator/**",)):
    snapshot = RepositorySnapshot(
        repo_root=str(tmp_path.resolve()),
        base_commit="abc123",
        working_tree_fingerprint="tree123",
        changed_paths=(),
    )
    return StructuredPlan.create(
        task="Add approval enforcement",
        repository=snapshot,
        policy=PolicyIdentity(fingerprint="policy123", sources=()),
        effective_constraints="Preserve unrelated work.",
        allowed_paths=allowed_paths,
        prohibited_operations=("commit", "push"),
        required_checks=("pytest",),
        proposal="## Scope\nImplement the approved boundary.",
    )


def init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def test_structured_plan_roundtrip_and_tamper_detection(tmp_path):
    plan = make_plan(tmp_path)

    assert StructuredPlan.from_json(plan.to_json()) == plan

    tampered = json.loads(plan.to_json())
    tampered["task"] = "Different task"
    with pytest.raises(ValueError, match="plan_id does not match"):
        StructuredPlan.from_json(json.dumps(tampered))


def test_approval_is_exact_single_use_and_detects_drift(tmp_path):
    plan = make_plan(tmp_path / "repo")
    approval = create_approval(plan, "reviewer")
    path = save_approval(approval, tmp_path / "state" / "approvals")

    validate_approval(
        plan,
        load_approval(path),
        current_base_commit="abc123",
        current_working_tree_fingerprint="tree123",
        current_policy_fingerprint="policy123",
    )
    consumed = consume_approval(path, approval)
    assert consumed.approval_id == approval.approval_id
    with pytest.raises(ValueError, match="already been consumed"):
        consume_approval(path, approval)
    with pytest.raises(ValueError, match="already been consumed"):
        validate_approval(
            plan,
            load_approval(path),
            current_base_commit="abc123",
            current_working_tree_fingerprint="tree123",
            current_policy_fingerprint="policy123",
        )

    with pytest.raises(ValueError, match="working tree"):
        validate_plan_state(
            plan,
            current_base_commit="abc123",
            current_working_tree_fingerprint="changed",
            current_policy_fingerprint="policy123",
        )


def test_policy_fingerprint_includes_constraints_and_source_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    policy_file = repo / "AGENTS.md"
    policy_file.write_text("preserve files")

    _, identity, constraints = load_policy_identity(
        repo, effective_constraints="read-only planning"
    )
    assert reload_policy_identity(identity.sources, constraints) == identity

    policy_file.write_text("updated policy")
    assert reload_policy_identity(identity.sources, constraints) != identity
    _, other, _ = load_policy_identity(
        repo, effective_constraints="different constraints"
    )
    assert other.fingerprint != identity.fingerprint


def test_repository_snapshot_detects_dirty_file_metadata_changes(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    clean = capture_repository_snapshot(repo)
    (repo / "tracked.txt").write_text("changed content\n")
    dirty = capture_repository_snapshot(repo)

    assert clean.base_commit == dirty.base_commit
    assert clean.working_tree_fingerprint != dirty.working_tree_fingerprint
    assert dirty.changed_paths == ("tracked.txt",)


def test_repository_snapshot_includes_nested_repository_changes(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    nested = repo / "nested"
    init_repo(nested)
    (nested / "tracked.txt").write_text("nested change\n")

    snapshot = capture_repository_snapshot(repo)

    assert "nested/tracked.txt" in snapshot.changed_paths


def test_structured_planning_binds_constraints_policy_and_repository(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    init_repo(repo)
    (repo / "AGENTS.md").write_text("repository policy")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "policy"], cwd=repo, check=True)
    received = {}

    def fake_reason(prompt, context=""):
        received["prompt"] = prompt
        received["context"] = context
        return "## Scope\nChange the approved files."

    monkeypatch.setattr(pipeline, "reason", fake_reason)
    monkeypatch.setattr(pipeline, "retrieve_context", lambda *args, **kwargs: "")

    plan = pipeline.plan_structured(
        "Add boundary checks",
        repo_root=str(repo),
        allowed_paths=["orchestrator/**"],
        effective_constraints="Do not commit.",
    )

    assert "repository policy" in received["context"]
    assert "Do not commit." in received["context"]
    assert plan.effective_constraints == "Do not commit."
    assert plan.repository.repo_root == str(repo.resolve())
    assert reload_policy_identity(
        plan.policy.sources, plan.effective_constraints
    ) == plan.policy


def test_allowed_paths_reject_escape_git_and_scope_violations():
    allowed = normalize_allowed_paths(
        ["orchestrator/**", "tests/test_phase1_approval.py", ".github/**"]
    )

    assert path_is_allowed("orchestrator/models.py", allowed)
    assert path_is_allowed("tests/test_phase1_approval.py", allowed)
    assert path_is_allowed(".github/workflows/check.yml", allowed)
    assert not path_is_allowed("SETUP.md", allowed)
    assert not path_is_allowed("src/deep/file.py", ("src/*",))
    with pytest.raises(ValueError, match="repository-relative"):
        normalize_allowed_paths(["../outside"])
    with pytest.raises(ValueError, match=".git"):
        normalize_allowed_paths([".git/config"])
    with pytest.raises(ValueError, match="exceed the approved scope"):
        validate_changed_paths({"orchestrator/models.py", "SETUP.md"}, allowed)


def test_approval_does_not_match_modified_plan(tmp_path):
    plan = make_plan(tmp_path / "repo")
    approval = create_approval(plan, "reviewer")
    modified = StructuredPlan.create(
        task=plan.task,
        repository=plan.repository,
        policy=plan.policy,
        effective_constraints=plan.effective_constraints,
        allowed_paths=("tests/**",),
        prohibited_operations=plan.prohibited_operations,
        required_checks=plan.required_checks,
        proposal=plan.proposal,
    )

    with pytest.raises(ValueError, match="drift or mismatch"):
        validate_approval(
            modified,
            approval,
            current_base_commit="abc123",
            current_working_tree_fingerprint="tree123",
            current_policy_fingerprint="policy123",
        )


def test_plan_schema_rejects_wrong_version(tmp_path):
    plan = make_plan(tmp_path)
    data = json.loads(plan.to_json())
    data["schema_version"] = 99

    with pytest.raises(ValueError, match="Unsupported plan schema"):
        StructuredPlan.from_json(json.dumps(data))


def test_approval_record_remains_valid_after_consumed_field_changes(tmp_path):
    plan = make_plan(tmp_path)
    approval = create_approval(plan, "reviewer")
    consumed = replace(
        approval,
        record_digest="pending",
        consumed_at="2026-01-01T00:00:00+00:00",
    )
    consumed = replace(consumed, record_digest=consumed.integrity_digest)

    assert consumed.approval_id == consumed.digest
    consumed.validate()

    tampered = replace(consumed, consumed_at=None)
    with pytest.raises(ValueError, match="record_digest"):
        tampered.validate()


def test_approval_record_cannot_be_stored_in_target_repository(tmp_path):
    repo = tmp_path / "repo"
    plan = make_plan(repo)
    approval = create_approval(plan, "reviewer")

    with pytest.raises(ValueError, match="outside the target repository"):
        save_approval(approval, repo / ".orchestrator" / "approvals")


def test_cli_approval_location_rejects_a_copied_record(tmp_path, monkeypatch):
    plan = make_plan(tmp_path / "repo")
    approval = create_approval(plan, "reviewer")
    monkeypatch.setenv("ORCHESTRATOR_STATE_DIR", str(tmp_path / "state"))
    copied = tmp_path / "copied-approval.json"

    with pytest.raises(ValueError, match="canonical approval record"):
        validate_approval_location(copied, approval)
