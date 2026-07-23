from pathlib import Path

import pytest

from orchestrator.context import load_agent_guidance, load_explicit_context


def test_agent_guidance_loads_workspace_to_target_precedence(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    target = repo / "ansible"
    target.mkdir(parents=True)
    (repo / ".git").mkdir()

    workspace_agents = workspace / "AGENTS.md"
    repo_agents = repo / "AGENTS.md"
    target_override = target / "AGENTS.override.md"
    workspace_agents.write_text("workspace rule")
    repo_agents.write_text("repository rule")
    target_override.write_text("target override")
    monkeypatch.setenv("RAG_SOURCE_DIRS", str(workspace))

    guidance = load_agent_guidance(repo, target)

    assert guidance.index("workspace rule") < guidance.index("repository rule")
    assert guidance.index("repository rule") < guidance.index("target override")
    assert str(workspace_agents) in guidance
    assert str(repo_agents) in guidance
    assert str(target_override) in guidance


def test_explicit_context_rejects_sensitive_filename(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    password_file = repo / "passwords.yml"
    password_file.write_text("not inspected by the loader")

    with pytest.raises(ValueError, match="Refusing sensitive context file"):
        load_explicit_context([password_file], repo)


def test_explicit_context_rejects_file_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')")

    with pytest.raises(ValueError, match="outside repository root"):
        load_explicit_context([outside], repo)
