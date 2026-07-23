from typer.testing import CliRunner

import cli
import mcp_server
import orchestrator.judge as judge
import orchestrator.pipeline as pipeline


def test_judge_reads_environment_when_called(monkeypatch):
    monkeypatch.setenv("JUDGE_ENABLED", "false")
    monkeypatch.setattr(
        judge,
        "reason",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("judge ran")),
    )

    assert judge.critique_and_revise("prompt", "draft") == "draft"


def test_explicit_setting_overrides_environment(monkeypatch):
    monkeypatch.setenv("JUDGE_ENABLED", "true")
    monkeypatch.setattr(
        judge,
        "reason",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("judge ran")),
    )

    assert judge.critique_and_revise("prompt", "draft", enabled=False) == "draft"


def test_pipeline_passes_per_call_judge_setting(monkeypatch):
    received = {}

    monkeypatch.setattr(pipeline, "classify", lambda prompt: "coding")
    monkeypatch.setattr(
        pipeline,
        "retrieve_context",
        lambda prompt, repo_root=None: "",
    )
    monkeypatch.setattr(pipeline, "code", lambda prompt, context: "draft")

    def fake_judge(prompt, draft, enabled=None, context=""):
        received["enabled"] = enabled
        return draft

    monkeypatch.setattr(pipeline, "critique_and_revise", fake_judge)

    result = pipeline.run("prompt", judge_enabled=False)

    assert result["final"] == "draft"
    assert received["enabled"] is False


def test_no_judge_cli_option_disables_judge_for_that_request(monkeypatch):
    received = {}

    def fake_run(
        prompt,
        context_path=None,
        judge_enabled=None,
        context_paths=None,
        repo_root=None,
    ):
        received["judge_enabled"] = judge_enabled
        return {
            "task_type": "coding",
            "context_used": False,
            "draft": "answer",
            "final": "answer",
        }

    monkeypatch.setattr(cli, "run", fake_run)

    result = CliRunner().invoke(cli.app, ["ask", "prompt", "--no-judge"])

    assert result.exit_code == 0
    assert received["judge_enabled"] is False


def test_mcp_ask_forwards_judge_choice(monkeypatch):
    received = {}

    def fake_run(
        prompt,
        context_path=None,
        judge_enabled=None,
        context_paths=None,
        repo_root=None,
    ):
        received["context_path"] = context_path
        received["context_paths"] = context_paths
        received["repo_root"] = repo_root
        received["judge_enabled"] = judge_enabled
        return {"final": "answer"}

    monkeypatch.setattr(mcp_server, "run", fake_run)

    result = mcp_server.ask_orchestrator(
        "prompt",
        context_path="example.py",
        use_judge=False,
    )

    assert result == "answer"
    assert received == {
        "context_path": "example.py",
        "context_paths": None,
        "repo_root": None,
        "judge_enabled": False,
    }
