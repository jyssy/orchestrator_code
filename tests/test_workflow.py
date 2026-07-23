from pathlib import Path

import pytest

from orchestrator.workflow import (
    build_codex_command,
    build_codex_prompt,
    build_copilot_prompt,
    resolve_target_repo,
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
    copilot_prompt = build_copilot_prompt(task, repo)

    for prompt in (codex_prompt, copilot_prompt):
        assert task in prompt
        assert str(repo) in prompt
        assert "plan_task" in prompt
        assert "ask_orchestrator" in prompt
        assert "same" in prompt
        assert "STOP" in prompt
        assert "Do not commit" in prompt
        assert "checks not run" in prompt


def test_codex_command_uses_workspace_sandbox_and_initial_prompt(tmp_path, monkeypatch):
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
        "workspace-write",
        "--ask-for-approval",
        "on-request",
    ]
    assert "Fix the parser" in command[-1]


def test_workflow_rejects_high_confidence_secret_material(tmp_path):
    with pytest.raises(ValueError, match="prohibited secret material"):
        build_codex_prompt(
            "Use -----BEGIN PRIVATE KEY----- in the fixture",
            Path(tmp_path),
        )
