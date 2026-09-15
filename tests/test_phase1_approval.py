import json
import os
import subprocess
import time
from dataclasses import replace

import pytest

from orchestrator import pipeline
from orchestrator.approval import (
    consume_approval,
    create_approval,
    find_latest_plan,
    find_latest_unconsumed_approval,
    load_approval,
    normalize_allowed_paths,
    normalize_denied_paths,
    path_is_allowed,
    path_is_denied,
    resolve_latest_execution_records,
    save_approval,
    save_plan,
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
from orchestrator.models import (
    PolicyIdentity,
    ReadOnlyContext,
    RepositorySnapshot,
    StructuredPlan,
)
from orchestrator.results import ComponentResult, ResultStatus
from orchestrator.workflow import validate_read_only_contexts


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


def test_structured_plan_roundtrip_binds_read_only_context(tmp_path):
    context = ReadOnlyContext(
        repository=RepositorySnapshot(
            repo_root=str((tmp_path / "infra").resolve()),
            base_commit="def456",
            working_tree_fingerprint="infra-tree",
            changed_paths=("existing.tf",),
        ),
        policy=PolicyIdentity(fingerprint="infra-policy", sources=()),
    )
    base = make_plan(tmp_path / "app")
    plan = StructuredPlan.create(
        task=base.task,
        repository=base.repository,
        policy=base.policy,
        effective_constraints=base.effective_constraints,
        allowed_paths=base.allowed_paths,
        prohibited_operations=base.prohibited_operations,
        required_checks=base.required_checks,
        proposal=base.proposal,
        read_only_contexts=(context,),
    )

    assert StructuredPlan.from_json(plan.to_json()) == plan
    assert "read_only_contexts" in json.loads(plan.to_json())


def test_deny_patterns_change_plan_digest_and_roundtrip(tmp_path):
    base = make_plan(tmp_path / "repo")
    denied = StructuredPlan.create(
        task=base.task,
        repository=base.repository,
        policy=base.policy,
        effective_constraints=base.effective_constraints,
        allowed_paths=base.allowed_paths,
        prohibited_operations=base.prohibited_operations,
        required_checks=base.required_checks,
        proposal=base.proposal,
        denied_paths=("infra/**",),
    )

    assert denied.plan_id != base.plan_id
    assert StructuredPlan.from_json(denied.to_json()) == denied
    assert "denied_paths" not in json.loads(base.to_json())


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

    monkeypatch.setattr(
        pipeline,
        "reason_result",
        lambda prompt, context="": ComponentResult(
            "specialist", ResultStatus.SUCCESS, fake_reason(prompt, context)
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "retrieve_context_result",
        lambda *args, **kwargs: ComponentResult(
            "retrieval", ResultStatus.SUCCESS, ""
        ),
    )

    plan = pipeline.plan_structured(
        "Add boundary checks",
        repo_root=str(repo),
        allowed_paths=["orchestrator/**"],
        effective_constraints="Do not commit.",
        denied_paths=["orchestrator/generated/**", "orchestrator/generated/**"],
    )

    assert "repository policy" in received["context"]
    assert "Do not commit." in received["context"]
    assert plan.effective_constraints == "Do not commit."
    assert plan.repository.repo_root == str(repo.resolve())
    assert plan.denied_paths == ("orchestrator/generated/**",)
    assert reload_policy_identity(
        plan.policy.sources, plan.effective_constraints
    ) == plan.policy


def test_structured_planning_binds_and_retrieves_separate_read_only_repo(
    tmp_path, monkeypatch
):
    app_repo = tmp_path / "app"
    infra_repo = tmp_path / "infra"
    init_repo(app_repo)
    init_repo(infra_repo)
    (infra_repo / "AGENTS.md").write_text("infra repository policy")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=infra_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "policy"], cwd=infra_repo, check=True)
    received = {}

    monkeypatch.setattr(
        pipeline,
        "reason_result",
        lambda prompt, context="": (
            received.update(context=context)
            or ComponentResult(
                "specialist",
                ResultStatus.SUCCESS,
                "## Scope\nUse the approved read-only context.",
            )
        ),
    )

    def fake_retrieval(prompt, repo_root=None):
        value = "infra deployment topology" if repo_root == str(infra_repo) else ""
        return ComponentResult("retrieval", ResultStatus.SUCCESS, value)

    monkeypatch.setattr(pipeline, "retrieve_context_result", fake_retrieval)

    plan = pipeline.plan_structured(
        "Update app configuration",
        repo_root=str(app_repo),
        allowed_paths=["tracked.txt"],
        effective_constraints="Do not edit infrastructure.",
        read_only_context_roots=[str(infra_repo)],
    )

    assert len(plan.read_only_contexts) == 1
    assert plan.read_only_contexts[0].repository.repo_root == str(infra_repo)
    assert "infra deployment topology" in received["context"]
    assert validate_read_only_contexts(plan)[str(infra_repo)].base_commit

    (infra_repo / "tracked.txt").write_text("drift\n")
    with pytest.raises(ValueError, match="Read-only context.*working tree"):
        validate_read_only_contexts(plan)


def test_structured_planning_requires_add_dir_to_name_separate_repo_root(tmp_path):
    app_repo = tmp_path / "app"
    infra_repo = tmp_path / "infra"
    init_repo(app_repo)
    init_repo(infra_repo)
    nested = infra_repo / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="must name a Git repository root"):
        pipeline.plan_structured(
            "Inspect infra",
            repo_root=str(app_repo),
            allowed_paths=["tracked.txt"],
            read_only_context_roots=[str(nested)],
        )

    with pytest.raises(ValueError, match="must be separate from the target"):
        pipeline.plan_structured(
            "Inspect app",
            repo_root=str(app_repo),
            allowed_paths=["tracked.txt"],
            read_only_context_roots=[str(app_repo)],
        )


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


def test_denied_paths_override_broad_allow_and_report_matching_pattern():
    allowed = normalize_allowed_paths(["**"])
    denied = normalize_denied_paths(
        ["infra/**", ".github/**", "**/migrations/**", "infra/**"]
    )

    assert denied == ("infra/**", ".github/**", "**/migrations/**")
    assert path_is_denied("infra/main.tf", denied)
    assert path_is_denied("accounts/migrations/0001_initial.py", denied)
    assert not path_is_denied("accounts/views.py", denied)
    assert not path_is_allowed("../outside", allowed)
    validate_changed_paths({"accounts/views.py"}, allowed, denied)
    with pytest.raises(ValueError, match=r"infra/main\.tf \(matched infra/\*\*\)"):
        validate_changed_paths({"infra/main.tf"}, allowed, denied)
    with pytest.raises(ValueError, match="repository-relative"):
        normalize_denied_paths(["../outside"])
    with pytest.raises(ValueError, match=".git"):
        normalize_denied_paths([".git/**"])


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


def test_find_latest_plan_scopes_to_repository_and_picks_newest(tmp_path):
    plans_dir = tmp_path / "state" / "plans"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"

    older_path = save_plan(make_plan(repo_a, allowed_paths=("a/**",)), plans_dir)
    newer_path = save_plan(make_plan(repo_a, allowed_paths=("b/**",)), plans_dir)
    save_plan(make_plan(repo_b), plans_dir)
    os.utime(older_path, (1_000_000, 1_000_000))
    os.utime(newer_path, (2_000_000, 2_000_000))

    resolved = find_latest_plan(repo_a, directory=plans_dir)

    assert resolved == newer_path


def test_find_latest_plan_raises_when_repository_has_no_plan(tmp_path):
    plans_dir = tmp_path / "state" / "plans"
    plans_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="No plan record"):
        find_latest_plan(tmp_path / "unrelated-repo", directory=plans_dir)


def test_find_latest_unconsumed_approval_excludes_consumed_and_scopes_to_repo(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    plan_a = make_plan(repo_a)

    save_approval(create_approval(plan_a, "reviewer"), approvals_dir)
    time.sleep(0.001)
    newer = create_approval(plan_a, "reviewer")
    newer_path = save_approval(newer, approvals_dir)
    time.sleep(0.001)
    consumed = create_approval(plan_a, "reviewer")
    consumed_path = save_approval(consumed, approvals_dir)
    consume_approval(consumed_path, consumed)
    save_approval(create_approval(make_plan(repo_b), "reviewer"), approvals_dir)

    resolved = find_latest_unconsumed_approval(repo_a, directory=approvals_dir)

    assert resolved == newer_path


def test_find_latest_unconsumed_approval_raises_when_none_available(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="No unconsumed approval"):
        find_latest_unconsumed_approval(tmp_path / "unrelated-repo", directory=approvals_dir)


def test_resolve_latest_execution_records_pairs_plan_and_approval(tmp_path):
    plans_dir = tmp_path / "state" / "plans"
    approvals_dir = tmp_path / "state" / "approvals"
    repo = tmp_path / "repo"
    plan = make_plan(repo)
    plan_path = save_plan(plan, plans_dir)
    approval_path = save_approval(create_approval(plan, "reviewer"), approvals_dir)

    resolved_plan_path, resolved_approval_path = resolve_latest_execution_records(
        repo, plans_directory=plans_dir, approvals_directory=approvals_dir
    )

    assert resolved_plan_path == plan_path
    assert resolved_approval_path == approval_path


def test_resolve_latest_execution_records_requires_matching_plan_file(tmp_path):
    plans_dir = tmp_path / "state" / "plans"
    approvals_dir = tmp_path / "state" / "approvals"
    plans_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    plan = make_plan(repo)
    save_approval(create_approval(plan, "reviewer"), approvals_dir)  # plan record not saved

    with pytest.raises(ValueError, match="Plan record for latest approval is missing"):
        resolve_latest_execution_records(
            repo, plans_directory=plans_dir, approvals_directory=approvals_dir
        )
