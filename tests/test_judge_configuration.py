from typer.testing import CliRunner

import cli
import mcp_server
import orchestrator.judge as judge
import orchestrator.pipeline as pipeline
from orchestrator.results import ComponentResult, ResultStatus


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

    monkeypatch.setattr(
        pipeline,
        "classify_result",
        lambda prompt: ComponentResult("router", ResultStatus.SUCCESS, "coding"),
    )
    monkeypatch.setattr(
        pipeline,
        "retrieve_context_result",
        lambda prompt, repo_root=None: ComponentResult(
            "retrieval", ResultStatus.SUCCESS, ""
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "code_result",
        lambda prompt, context: ComponentResult(
            "specialist", ResultStatus.SUCCESS, "draft"
        ),
    )

    def fake_judge(prompt, draft, enabled=None, context=""):
        received["enabled"] = enabled
        return ComponentResult("judge", ResultStatus.SUCCESS, draft)

    monkeypatch.setattr(pipeline, "critique_and_revise_result", fake_judge)

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
        effective_constraints=None,
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
    assert "Reviewer (Qwen3-Coder-Next)" in result.stdout
    assert "Judge (" not in result.stdout


def test_mcp_ask_forwards_judge_choice(monkeypatch):
    received = {}

    def fake_run(
        prompt,
        context_path=None,
        judge_enabled=None,
        context_paths=None,
        repo_root=None,
        effective_constraints=None,
    ):
        received["context_path"] = context_path
        received["context_paths"] = context_paths
        received["repo_root"] = repo_root
        received["judge_enabled"] = judge_enabled
        received["effective_constraints"] = effective_constraints
        return {
            "final": "answer",
            "model_roles": {"reviewer": "Qwen3-Coder-Next", "judge": None},
        }

    monkeypatch.setattr(mcp_server, "run", fake_run)

    result = mcp_server.ask_orchestrator(
        "prompt",
        context_path="example.py",
        use_judge=False,
    )

    assert result == "Reviewer (Qwen3-Coder-Next)\n\nanswer"
    assert received == {
        "context_path": "example.py",
        "context_paths": None,
        "repo_root": None,
        "judge_enabled": False,
        "effective_constraints": "",
    }


def test_mcp_structured_plan_forwards_constraints_and_allowed_paths(monkeypatch):
    received = {}

    class FakePlan:
        def to_json(self):
            return '{"schema_version": 1}'

    def fake_plan_structured(prompt, **kwargs):
        received.update(prompt=prompt, **kwargs)
        return FakePlan()

    monkeypatch.setattr(mcp_server, "plan_structured", fake_plan_structured)

    result = mcp_server.plan_task_structured(
        "prompt",
        repo_root="/repo",
        allowed_paths=["src/**"],
        effective_constraints="Do not commit.",
    )

    assert result == '{"schema_version": 1}'
    assert received == {
        "prompt": "prompt",
        "repo_root": "/repo",
        "allowed_paths": ["src/**"],
        "effective_constraints": "Do not commit.",
        "context_path": None,
        "context_paths": None,
    }


def test_mcp_plan_identifies_the_reviewer_model(monkeypatch):
    monkeypatch.setattr(mcp_server, "plan", lambda *args, **kwargs: "plan text")

    assert mcp_server.plan_task("prompt") == (
        "Reviewer (gpt-oss-120b)\n\nplan text"
    )


def test_mcp_plan_label_tracks_configured_reasoning_model(monkeypatch):
    monkeypatch.setenv("REALMS_REASONING_MODEL", "replacement-reasoner")
    monkeypatch.setattr(mcp_server, "plan", lambda *args, **kwargs: "plan text")

    assert mcp_server.plan_task("prompt") == (
        "Reviewer (replacement-reasoner)\n\nplan text"
    )
