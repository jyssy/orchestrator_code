from typer.testing import CliRunner

import cli


def test_plan_command_prints_plan_without_execution_or_approval(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    context_file = repo / "example.py"
    context_file.write_text("VALUE = 1\n")

    received = {}

    def fake_plan(prompt, context_path=None, repo_root=None):
        received.update(
            prompt=prompt,
            context_path=context_path,
            repo_root=repo_root,
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
    assert "Read-only planning" in result.stdout
    assert received == {
        "prompt": "Add request validation",
        "context_path": str(context_file),
        "repo_root": str(repo.resolve()),
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
