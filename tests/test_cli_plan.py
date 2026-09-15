import subprocess

from typer.testing import CliRunner

import cli
from orchestrator.approval import load_approval, save_plan
from orchestrator.context import capture_repository_snapshot, load_policy_identity
from orchestrator.models import (
    PolicyIdentity,
    ReadOnlyContext,
    RepositorySnapshot,
    StructuredPlan,
)


def test_plan_command_prints_plan_without_execution_or_approval(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    context_file = repo / "example.py"
    context_file.write_text("VALUE = 1\n")

    received = {}

    def fake_plan(
        prompt,
        context_path=None,
        repo_root=None,
        effective_constraints=None,
    ):
        received.update(
            prompt=prompt,
            context_path=context_path,
            repo_root=repo_root,
            effective_constraints=effective_constraints,
        )
        return "## Scope\n- Read-only planning"

    def unexpected(*args, **kwargs):
        raise AssertionError("plan command entered an execution or approval path")

    monkeypatch.setattr(cli, "run_plan", fake_plan)
    monkeypatch.setattr(cli, "run", unexpected)
    monkeypatch.setattr(cli, "build_codex_command", unexpected)
    monkeypatch.setattr(cli.subprocess, "run", unexpected)
    monkeypatch.setattr(cli.typer, "confirm", unexpected)

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "Add request validation",
            "--repo-root",
            str(repo),
            "--file",
            str(context_file),
        ],
    )

    assert result.exit_code == 0
    assert "Plan (no changes made)" in result.stdout
    assert "Reviewer (gpt-oss-120b)" in result.stdout
    assert "Read-only planning" in result.stdout
    assert received == {
        "prompt": "Add request validation",
        "context_path": str(context_file),
        "repo_root": str(repo.resolve()),
        "effective_constraints": cli.DEFAULT_EFFECTIVE_CONSTRAINTS,
    }


def test_plan_command_requires_a_git_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "run_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("planner ran for a non-repository")
        ),
    )

    result = CliRunner().invoke(
        cli.app,
        ["plan", "Describe a change", "--repo-root", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "not inside a Git repository" in result.stderr


def test_plan_command_stores_add_dir_in_structured_plan(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    context_repo = tmp_path / "infra"
    repo.mkdir()
    context_repo.mkdir()
    (repo / ".git").mkdir()
    (context_repo / ".git").mkdir()
    received = {}
    context = ReadOnlyContext(
        repository=RepositorySnapshot(
            str(context_repo.resolve()), "def", "infra-tree", ()
        ),
        policy=PolicyIdentity("infra-policy", ()),
    )
    structured = StructuredPlan.create(
        task="Use infra context",
        repository=RepositorySnapshot(str(repo.resolve()), "abc", "tree", ()),
        policy=PolicyIdentity("policy", ()),
        effective_constraints=cli.DEFAULT_EFFECTIVE_CONSTRAINTS,
        allowed_paths=("**",),
        prohibited_operations=("commit",),
        required_checks=("pytest",),
        proposal="Read infra without editing it.",
        read_only_contexts=(context,),
        denied_paths=("infra/**",),
    )

    def fake_structured(*args, **kwargs):
        received.update(kwargs)
        return structured

    monkeypatch.setattr(cli, "run_structured_plan", fake_structured)
    monkeypatch.setattr(cli, "save_plan", lambda plan: tmp_path / "plan.json")

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "Use infra context",
            "--repo-root",
            str(repo),
            "--allow",
            "**",
            "--add-dir",
            str(context_repo),
            "--deny",
            "infra/**",
        ],
    )

    assert result.exit_code == 0
    assert received["read_only_context_roots"] == [str(context_repo)]
    assert received["denied_paths"] == ["infra/**"]
    assert "Read-only context:" in result.stdout
    assert context_repo.name in result.stdout
    assert "Denied write path: infra/**" in result.stdout


def test_plan_command_requires_allow_when_add_dir_is_used(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "Describe a change",
            "--repo-root",
            str(repo),
            "--add-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "--add-dir requires --allow" in result.stderr


def test_plan_command_requires_allow_when_deny_is_used(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "Describe a change",
            "--repo-root",
            str(repo),
            "--deny",
            "infra/**",
        ],
    )

    assert result.exit_code == 2
    assert "--deny requires --allow" in result.stderr


def test_approve_and_execute_print_only_lifecycle(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo, check=True
    )
    (repo / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    snapshot = capture_repository_snapshot(repo)
    _, policy, constraints = load_policy_identity(
        repo, effective_constraints=cli.DEFAULT_EFFECTIVE_CONSTRAINTS
    )
    plan = StructuredPlan.create(
        task="Change tracked file",
        repository=snapshot,
        policy=policy,
        effective_constraints=constraints,
        allowed_paths=("tracked.txt",),
        prohibited_operations=("commit",),
        required_checks=("pytest",),
        proposal="Change the approved file.",
    )
    state = tmp_path / "state"
    plan_path = save_plan(plan, state / "plans")
    monkeypatch.setenv("ORCHESTRATOR_STATE_DIR", str(state))
    monkeypatch.setattr(
        "orchestrator.workflow.shutil.which", lambda executable: "/bin/echo"
    )

    approve = CliRunner().invoke(
        cli.app, ["approve", str(plan_path), "--approved-by", "reviewer"]
    )

    assert approve.exit_code == 0
    approval_files = list((state / "approvals").glob("*.json"))
    assert len(approval_files) == 1

    execute = CliRunner().invoke(
        cli.app,
        ["execute", str(plan_path), str(approval_files[0]), "--print-only"],
    )

    assert execute.exit_code == 0
    assert "workspace-write" in execute.stdout
    assert load_approval(approval_files[0]).consumed_at is None


def test_approve_and_execute_accept_latest_instead_of_explicit_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    snapshot = capture_repository_snapshot(repo)
    _, policy, constraints = load_policy_identity(
        repo, effective_constraints=cli.DEFAULT_EFFECTIVE_CONSTRAINTS
    )
    plan = StructuredPlan.create(
        task="Change tracked file",
        repository=snapshot,
        policy=policy,
        effective_constraints=constraints,
        allowed_paths=("tracked.txt",),
        prohibited_operations=("commit",),
        required_checks=("pytest",),
        proposal="Change the approved file.",
    )
    state = tmp_path / "state"
    save_plan(plan, state / "plans")
    monkeypatch.setenv("ORCHESTRATOR_STATE_DIR", str(state))
    monkeypatch.setattr(
        "orchestrator.workflow.shutil.which", lambda executable: "/bin/echo"
    )

    approve = CliRunner().invoke(
        cli.app,
        ["approve", "--latest", "--repo-root", str(repo), "--approved-by", "reviewer"],
    )

    assert approve.exit_code == 0
    assert "Using latest plan" in approve.stdout
    approval_files = list((state / "approvals").glob("*.json"))
    assert len(approval_files) == 1

    execute = CliRunner().invoke(
        cli.app,
        ["execute", "--latest", "--repo-root", str(repo), "--print-only"],
    )

    assert execute.exit_code == 0
    assert "Using latest approval" in execute.stdout
    assert "workspace-write" in execute.stdout
    assert load_approval(approval_files[0]).consumed_at is None


def test_approve_rejects_latest_combined_with_explicit_plan_file(tmp_path):
    result = CliRunner().invoke(
        cli.app, ["approve", str(tmp_path / "plan.json"), "--latest"]
    )

    assert result.exit_code == 2
    assert "Pass either a plan file or --latest" in result.stderr


def test_execute_rejects_latest_combined_with_explicit_paths(tmp_path):
    result = CliRunner().invoke(
        cli.app,
        [
            "execute",
            str(tmp_path / "plan.json"),
            str(tmp_path / "approval.json"),
            "--latest",
        ],
    )

    assert result.exit_code == 2
    assert "Pass either explicit plan/approval files or --latest" in result.stderr


def test_execute_requires_both_paths_or_latest(tmp_path):
    result = CliRunner().invoke(cli.app, ["execute", str(tmp_path / "plan.json")])

    assert result.exit_code == 2
    assert "Provide both plan and approval file paths, or pass --latest" in result.stderr
