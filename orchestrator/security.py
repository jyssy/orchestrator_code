"""Safety filters for files that must never be sent to remote model APIs."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path


INDEXABLE_EXTENSIONS = {
    ".cfg",
    ".ini",
    ".j2",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".tf",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".terragrunt-cache",
    ".tox",
    ".uv",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

SENSITIVE_FILENAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "credentials.yml",
    "credentials.yaml",
    "password.yml",
    "password.yaml",
    "passwords.yml",
    "passwords.yaml",
    "secret.json",
    "secret.yml",
    "secret.yaml",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
    "terraform.tfstate",
    "vault.yml",
    "vault.yaml",
}

SENSITIVE_SUFFIXES = {
    ".der",
    ".jks",
    ".key",
    ".kdbx",
    ".p12",
    ".pem",
    ".pfx",
    ".tfplan",
    ".tfstate",
}

SENSITIVE_DIRECTORY_NAMES = {
    "credentials",
    "private_keys",
    "secrets",
}

_HIGH_CONFIDENCE_SECRET_PATTERNS = {
    "Ansible Vault ciphertext": re.compile(r"(?m)^\$ANSIBLE_VAULT;"),
    "private key material": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}


def excluded_directory(path: Path) -> bool:
    """Return whether a directory should always be pruned from an index scan."""
    return path.name.lower() in EXCLUDED_DIRECTORY_NAMES


def sensitive_path_reason(path: Path) -> str | None:
    """Return a reason when a path is too sensitive to read or index."""
    name = path.name.lower()

    if any(part.lower() in SENSITIVE_DIRECTORY_NAMES for part in path.parts[:-1]):
        return "sensitive directory"
    if name.startswith(".env") and name not in {".env.example", ".env.sample"}:
        return "environment file"
    if name in SENSITIVE_FILENAMES:
        return "sensitive filename"
    if any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return "sensitive file type"
    if ".tfstate." in name:
        return "Terraform state"
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return "private key filename"
    return None


def sensitive_content_reason(text: str) -> str | None:
    """Detect only high-confidence secret material to limit false positives."""
    for reason, pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS.items():
        if pattern.search(text):
            return reason
    return None


def load_ignore_patterns(source: Path) -> list[str]:
    """Load simple git-style patterns from the source root's .orchestratorignore."""
    ignore_file = source / ".orchestratorignore"
    if not ignore_file.is_file():
        return []

    patterns: list[str] = []
    for raw_line in ignore_file.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def matches_ignore_patterns(relative_path: Path, patterns: list[str]) -> bool:
    """
    Apply ordered .orchestratorignore patterns.

    Supports comments, negation, basename patterns, anchored paths, and ** globs.
    """
    relative = relative_path.as_posix()
    ignored = False

    for raw_pattern in patterns:
        negated = raw_pattern.startswith("!")
        pattern = raw_pattern[1:] if negated else raw_pattern
        pattern = pattern.lstrip("/")

        if pattern.endswith("/"):
            directory = pattern.rstrip("/")
            matched = relative == directory or relative.startswith(f"{directory}/")
        elif "/" not in pattern:
            matched = any(fnmatch.fnmatch(part, pattern) for part in relative_path.parts)
        else:
            matched = fnmatch.fnmatch(relative, pattern) or relative_path.match(pattern)

        if matched:
            ignored = not negated

    return ignored
