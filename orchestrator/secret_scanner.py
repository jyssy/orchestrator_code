"""Fail-closed adapter for the local Gitleaks secret scanner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SecretScannerError(RuntimeError):
    """Base class for scanner failures safe to expose to callers."""


class SecretScannerUnavailable(SecretScannerError):
    """The required local scanner cannot be executed."""


class SecretScannerTimeout(SecretScannerError):
    """The scanner did not finish within the configured bound."""


class SecretScannerMalformedOutput(SecretScannerError):
    """The scanner result could not be interpreted safely."""


@dataclass(frozen=True)
class SecretFinding:
    """Redacted, non-secret scanner metadata."""

    rule_id: str
    line: int | None = None


class SecretScannerFindings(SecretScannerError):
    """One or more findings blocked the payload."""

    def __init__(self, findings: tuple[SecretFinding, ...]):
        self.findings = findings
        rules = sorted({finding.rule_id for finding in findings})
        rule_summary = ", ".join(rules[:8]) or "unclassified"
        super().__init__(
            f"Secret scanner blocked model payload: {len(findings)} finding(s); "
            f"rule(s): {rule_summary}"
        )


def _scanner_executable() -> str:
    configured = os.getenv("GITLEAKS_PATH", "gitleaks").strip()
    if not configured:
        raise SecretScannerUnavailable("Gitleaks path is not configured")
    executable = shutil.which(configured)
    if executable is None:
        raise SecretScannerUnavailable("Required local Gitleaks scanner is unavailable")
    return executable


def _timeout_seconds() -> float:
    raw = os.getenv("SECRET_SCAN_TIMEOUT_SECONDS", "15").strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise SecretScannerError("Secret scanner timeout is invalid") from exc
    if timeout <= 0 or timeout > 120:
        raise SecretScannerError(
            "Secret scanner timeout must be between 0 and 120 seconds"
        )
    return timeout


def _parse_findings(report_path: Path) -> tuple[SecretFinding, ...]:
    try:
        parsed = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SecretScannerMalformedOutput(
            "Gitleaks reported findings without valid metadata"
        ) from exc
    if not isinstance(parsed, list) or not parsed:
        raise SecretScannerMalformedOutput(
            "Gitleaks reported findings without valid metadata"
        )

    findings: list[SecretFinding] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise SecretScannerMalformedOutput("Gitleaks metadata has an invalid shape")
        rule = item.get("RuleID")
        line = item.get("StartLine")
        if not isinstance(rule, str) or not rule.strip():
            raise SecretScannerMalformedOutput(
                "Gitleaks metadata omits a rule identifier"
            )
        if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
            raise SecretScannerMalformedOutput(
                "Gitleaks metadata has an invalid line number"
            )
        findings.append(SecretFinding(rule_id=rule.strip()[:128], line=line))
    return tuple(findings)


def scan_text(text: str) -> None:
    """Scan text over stdin; return only when Gitleaks completed with no findings."""
    executable = _scanner_executable()
    try:
        with tempfile.TemporaryDirectory(
            prefix="orchestrator-secret-scan-"
        ) as directory:
            report_path = Path(directory) / "findings.json"
            scanner_environment = {
                "HOME": directory,
                "LANG": os.getenv("LANG", "C"),
                "PATH": os.getenv("PATH", ""),
                "TMPDIR": directory,
            }
            try:
                result = subprocess.run(
                    [
                        executable,
                        "stdin",
                        "--no-banner",
                        "--redact=100",
                        "--report-format=json",
                        f"--report-path={report_path}",
                    ],
                    input=text,
                    capture_output=True,
                    cwd=directory,
                    env=scanner_environment,
                    text=True,
                    timeout=_timeout_seconds(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SecretScannerTimeout("Gitleaks scan timed out") from exc
            except OSError as exc:
                raise SecretScannerUnavailable(
                    "Gitleaks could not be executed"
                ) from exc

            if result.returncode == 0:
                return
            if result.returncode == 1:
                raise SecretScannerFindings(_parse_findings(report_path))
            raise SecretScannerError("Gitleaks scan failed closed")
    except SecretScannerError:
        raise
    except OSError as exc:
        raise SecretScannerError(
            "Secure scanner workspace could not be created"
        ) from exc
