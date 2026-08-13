import stat
from pathlib import Path

import pytest

from orchestrator import (
    egress_guard,
    model_gateway,
    pipeline,
    secret_scanner,
    specialists,
)
from orchestrator.context import load_policy_identity, reload_policy_identity
from orchestrator.results import ComponentResult, ResultStatus
from orchestrator.security import (
    DataClassification,
    ModelEgressPolicyError,
    load_data_classification,
)


def _scanner(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-gitleaks"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_scanner_unavailable_fails_closed_without_payload_in_error(
    tmp_path, monkeypatch
):
    sentinel = "do-not-echo-this-value"
    monkeypatch.setenv("GITLEAKS_PATH", str(tmp_path / "missing-gitleaks"))

    with pytest.raises(secret_scanner.SecretScannerUnavailable) as captured:
        secret_scanner.scan_text(sentinel)

    assert sentinel not in str(captured.value)


def test_scanner_findings_are_redacted_metadata_only(tmp_path, monkeypatch):
    body = (
        'report=\'\'; for argument in "$@"; do case "$argument" in '
        "--report-path=*) report=${argument#*=} ;; esac; done; "
        'printf \'[{"RuleID":"generic-api-key","StartLine":7,'
        '"Secret":"REDACTED"}]\' > "$report"; exit 1'
    )
    scanner = _scanner(tmp_path, body)
    sentinel = "actual-sensitive-value"
    monkeypatch.setenv("GITLEAKS_PATH", str(scanner))

    with pytest.raises(secret_scanner.SecretScannerFindings) as captured:
        secret_scanner.scan_text(sentinel)

    message = str(captured.value)
    assert sentinel not in message
    assert "REDACTED" not in message
    assert "generic-api-key" in message
    assert captured.value.findings == (
        secret_scanner.SecretFinding(rule_id="generic-api-key", line=7),
    )


def test_scanner_timeout_and_malformed_output_fail_closed(tmp_path, monkeypatch):
    slow = _scanner(tmp_path, "sleep 1; exit 0")
    monkeypatch.setenv("GITLEAKS_PATH", str(slow))
    monkeypatch.setenv("SECRET_SCAN_TIMEOUT_SECONDS", "0.01")
    with pytest.raises(secret_scanner.SecretScannerTimeout):
        secret_scanner.scan_text("ordinary input")

    body = (
        'report=\'\'; for argument in "$@"; do case "$argument" in '
        "--report-path=*) report=${argument#*=} ;; esac; done; "
        "printf 'not-json' > \"$report\"; exit 1"
    )
    malformed = _scanner(tmp_path, body)
    monkeypatch.setenv("GITLEAKS_PATH", str(malformed))
    monkeypatch.setenv("SECRET_SCAN_TIMEOUT_SECONDS", "15")
    with pytest.raises(secret_scanner.SecretScannerMalformedOutput):
        secret_scanner.scan_text("ordinary input")


def test_gateway_scans_nested_completion_payload_and_cannot_be_bypassed(monkeypatch):
    calls = []

    def reject_marker(text):
        calls.append(text)
        if "nested-sensitive-marker" in text:
            raise secret_scanner.SecretScannerFindings(
                (secret_scanner.SecretFinding("test-rule", 1),)
            )

    monkeypatch.setattr(egress_guard, "scan_text", reject_marker)
    monkeypatch.setattr(
        model_gateway.litellm,
        "completion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider was called")),
    )

    with (
        egress_guard.egress_scope(DataClassification.REMOTE_APPROVED),
        pytest.raises(egress_guard.ModelEgressBlocked) as captured,
    ):
        model_gateway.completion(
            model="openai/example",
            messages=[{"role": "user", "content": "safe"}],
            api_base="https://models.invalid/v1",
            api_key="credential-is-not-model-content",
            remote=True,
            tools=[{"description": "nested-sensitive-marker"}],
        )

    assert calls
    assert "nested-sensitive-marker" not in str(captured.value)


def test_local_only_allows_local_and_denies_remote_before_provider(monkeypatch):
    class Response:
        pass

    calls = []
    monkeypatch.setattr(egress_guard, "scan_text", lambda text: None)
    monkeypatch.setattr(
        model_gateway.litellm,
        "completion",
        lambda **kwargs: calls.append(kwargs) or Response(),
    )
    arguments = {
        "model": "ollama/example",
        "messages": [{"role": "user", "content": "safe"}],
        "api_base": "http://localhost:11434",
    }

    with egress_guard.egress_scope(DataClassification.LOCAL_ONLY):
        assert model_gateway.completion(**arguments, remote=False).__class__ is Response
        with pytest.raises(egress_guard.ModelEgressBlocked, match="remote"):
            model_gateway.completion(**arguments, remote=True)

    with egress_guard.egress_scope(DataClassification.REMOTE_APPROVED):
        assert model_gateway.completion(**arguments, remote=True).__class__ is Response

    with (
        egress_guard.egress_scope(DataClassification.DENY_MODEL),
        pytest.raises(egress_guard.ModelEgressBlocked, match="all model"),
    ):
        model_gateway.completion(**arguments, remote=False)

    assert len(calls) == 2


def test_repository_classification_is_explicit_and_policy_drift_is_bound(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert load_data_classification(repo) is DataClassification.LOCAL_ONLY

    _, before, constraints = load_policy_identity(repo)
    policy_file = repo / ".orchestrator-policy.toml"
    policy_file.write_text('[model_egress]\nclassification = "remote-approved"\n')
    assert load_data_classification(repo) is DataClassification.REMOTE_APPROVED
    assert reload_policy_identity(before.sources, constraints) != before

    policy_file.write_text('[model_egress]\nclassification = "unknown"\n')
    with pytest.raises(ModelEgressPolicyError):
        load_data_classification(repo)


def test_pipeline_scans_each_early_context_source(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "remote-approved"\n'
    )
    (repo / "AGENTS.md").write_text("agent policy")
    context_file = repo / "example.py"
    context_file.write_text("VALUE = 1")
    sources = []

    monkeypatch.setattr(
        pipeline,
        "guard_text",
        lambda text, *, source, **kwargs: sources.append(source),
    )
    monkeypatch.setattr(
        pipeline,
        "classify_result",
        lambda prompt: ComponentResult("router", ResultStatus.SUCCESS, "coding"),
    )
    monkeypatch.setattr(
        pipeline,
        "retrieve_context_result",
        lambda *args, **kwargs: ComponentResult(
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

    result = pipeline.run(
        "safe task",
        repo_root=str(repo),
        context_path=str(context_file),
        effective_constraints="safe constraint",
        judge_enabled=False,
    )

    assert result["final"] == "draft"
    assert {
        "user task",
        "effective agent guidance",
        "effective caller constraints",
        "explicit repository context",
    }.issubset(sources)


def test_nested_context_uses_the_most_restrictive_repository_policy(tmp_path):
    parent = tmp_path / "parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    (parent / ".git").mkdir()
    (nested / ".git").mkdir()
    (parent / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "remote-approved"\n'
    )
    (nested / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "local-only"\n'
    )
    context_file = nested / "context.py"
    context_file.write_text("VALUE = 1")

    assert pipeline._request_data_classification(
        parent, [str(context_file)]
    ) is DataClassification.LOCAL_ONLY

    _, identity, constraints = load_policy_identity(
        parent,
        target_path=context_file,
        model_egress_roots=[parent, nested],
    )
    (nested / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "deny-model"\n'
    )
    assert reload_policy_identity(identity.sources, constraints) != identity


def test_only_gateway_imports_provider_clients():
    root = Path(__file__).parents[1] / "orchestrator"
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "model_gateway.py":
            continue
        text = path.read_text()
        if "import litellm" in text or "import httpx" in text:
            offenders.append(path.name)
    assert offenders == []


def test_secret_scan_failure_never_triggers_local_fallback(monkeypatch):
    monkeypatch.setattr(
        specialists,
        "_realms",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            egress_guard.ModelContentBlocked("redacted scan failure")
        ),
    )
    monkeypatch.setattr(
        specialists,
        "_local",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local fallback bypassed scanner failure")
        ),
    )

    with pytest.raises(egress_guard.ModelContentBlocked):
        specialists.code("safe prompt")
