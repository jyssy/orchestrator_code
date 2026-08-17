from pathlib import Path

import pytest

from orchestrator.models import PolicyIdentity, RepositorySnapshot, StructuredPlan
from orchestrator.workflow import (
    Executor,
    build_claude_command,
    build_codex_command,
    build_codex_prompt,
    build_execution_command,
    build_execution_prompt,
    resolve_target_repo,
    validate_execution_result,
)


def test_resolve_target_repo_accepts_nested_directory(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "src" / "package"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    assert resolve_target_repo(nested) == repo.resolve()


def test_resolve_target_repo_rejects_non_repository(tmp_path):
    with pytest.raises(ValueError, match="not inside a Git repository"):
        resolve_target_repo(tmp_path)


def test_workflow_prompts_lock_repo_approval_and_handoff(tmp_path):
    repo = tmp_path / "repo"
    task = "Add request validation"

    codex_prompt = build_codex_prompt(task, repo)

    assert task in codex_prompt
    assert str(repo) in codex_prompt
    assert "plan_task" in codex_prompt
    assert "STOP" in codex_prompt
    assert "separate" in codex_prompt
    assert "Do not commit" in codex_prompt


def test_codex_planning_command_uses_read_only_sandbox(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    monkeypatch.setattr(
        "orchestrator.workflow.shutil.which",
        lambda executable: "/usr/local/bin/codex",
    )

    command = build_codex_command("Fix the parser", repo)

    assert command[:7] == [
        "/usr/local/bin/codex",
        "-C",
        str(repo),
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "on-request",
    ]
    assert "Fix the parser" in command[-1]


def test_claude_planning_command_forwards_add_dir(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    monkeypatch.setattr(
        "orchestrator.workflow.shutil.which",
        lambda executable: "/usr/local/bin/claude",
    )

    command = build_claude_command(
        "Fix the parser", repo, add_dirs=["/extra/one", "/extra/two"]
    )

    assert "--add-dir" in command
    add_dir_index = command.index("--add-dir")
    assert command[add_dir_index + 1 : add_dir_index + 3] == [
        "/extra/one",
        "/extra/two",
    ]
    assert command[-2] == "--"


def test_claude_planning_command_omits_add_dir_when_unset(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    monkeypatch.setattr(
        "orchestrator.workflow.shutil.which",
        lambda executable: "/usr/local/bin/claude",
    )

    command = build_claude_command("Fix the parser", repo)

    assert "--add-dir" not in command


def test_workflow_rejects_high_confidence_secret_material(tmp_path):
    with pytest.raises(ValueError, match="prohibited secret material"):
        build_codex_prompt(
            "Use -----BEGIN PRIVATE KEY----- in the fixture",
            Path(tmp_path),
        )


def test_approved_execution_command_is_separate_and_write_capable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.workflow.shutil.which",
        lambda executable: f"/usr/local/bin/{executable}",
    )
    plan = StructuredPlan.create(
        task="Fix the parser",
        repository=RepositorySnapshot(str(tmp_path), "abc", "tree", ()),
        policy=PolicyIdentity("policy", ()),
        effective_constraints="Preserve unrelated work.",
        allowed_paths=("orchestrator/**",),
        prohibited_operations=("commit",),
        required_checks=("pytest",),
        proposal="Proposal text is advisory.",
    )

    command = build_execution_command(plan, Executor.CODEX)
    prompt = build_execution_prompt(plan)

    assert command[4] == "workspace-write"
    assert command[-1] == prompt
    assert plan.plan_id in prompt
    assert "ask_orchestrator" in prompt
    assert "orchestrator/**" in prompt
    assert "Proposal text is advisory" not in prompt


def test_execution_result_preserves_unrelated_preexisting_changes(tmp_path):
    plan = StructuredPlan.create(
        task="Add models",
        repository=RepositorySnapshot(
            str(tmp_path), "abc", "before", ("notes.txt",)
        ),
        policy=PolicyIdentity("policy", ()),
        effective_constraints="",
        allowed_paths=("orchestrator/**",),
        prohibited_operations=("commit",),
        required_checks=("pytest",),
        proposal="Add the approved files.",
    )
    before = plan.repository
    after = RepositorySnapshot(
        str(tmp_path), "abc", "after", ("notes.txt", "orchestrator/models.py")
    )

    changed = validate_execution_result(
        plan,
        before,
        after,
        before_metadata={"notes.txt": (1, 2, 3)},
        after_metadata={
            "notes.txt": (1, 2, 3),
            "orchestrator/models.py": (1, 10, 4),
        },
    )

    assert changed == {"orchestrator/models.py"}


def test_execution_result_rejects_out_of_scope_change(tmp_path):
    plan = StructuredPlan.create(
        task="Add models",
        repository=RepositorySnapshot(str(tmp_path), "abc", "before", ()),
        policy=PolicyIdentity("policy", ()),
        effective_constraints="",
        allowed_paths=("orchestrator/**",),
        prohibited_operations=("commit",),
        required_checks=("pytest",),
        proposal="Add the approved files.",
    )
    after = RepositorySnapshot(str(tmp_path), "abc", "after", ("SETUP.md",))

    with pytest.raises(ValueError, match="outside the approved scope"):
        validate_execution_result(
            plan,
            plan.repository,
            after,
            before_metadata={},
            after_metadata={"SETUP.md": (1, 10, 4)},
        )
