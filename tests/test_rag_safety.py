from pathlib import Path

import orchestrator.rag as rag
from orchestrator.security import (
    matches_ignore_patterns,
    sensitive_content_reason,
    sensitive_path_reason,
)


def test_sensitive_path_and_content_detection():
    assert sensitive_path_reason(Path("ansible/passwords.yml"))
    assert sensitive_path_reason(Path(".env"))
    assert sensitive_path_reason(Path("terraform.tfstate.backup"))
    assert sensitive_path_reason(Path("id_ed25519"))
    assert sensitive_path_reason(Path(".env.example")) is None
    assert sensitive_content_reason("$ANSIBLE_VAULT;1.1;AES256\nciphertext")
    assert sensitive_content_reason("-----BEGIN PRIVATE KEY-----\nmaterial")
    assert sensitive_content_reason("ordinary application code") is None


def test_orchestratorignore_patterns_support_negation():
    patterns = ["docs/**", "!docs/public.md", "*.generated.py"]

    assert matches_ignore_patterns(Path("docs/private.md"), patterns)
    assert not matches_ignore_patterns(Path("docs/public.md"), patterns)
    assert matches_ignore_patterns(Path("src/model.generated.py"), patterns)


def test_scan_excludes_generated_ignored_and_sensitive_files(tmp_path):
    source = tmp_path / "workspace"
    safe_file = source / "repo" / "main.py"
    safe_file.parent.mkdir(parents=True)
    safe_file.write_text("def safe():\n    return True\n")

    generated = source / "repo" / ".venv" / "lib" / "generated.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated = True")

    password_file = source / "repo" / "ansible" / "passwords.yml"
    password_file.parent.mkdir(parents=True)
    password_file.write_text("must_not_be_read: value")

    vault_file = source / "repo" / "ansible" / "vars.yml"
    vault_file.write_text("$ANSIBLE_VAULT;1.1;AES256\nciphertext")

    ignored_file = source / "repo" / "notes" / "private.md"
    ignored_file.parent.mkdir(parents=True)
    ignored_file.write_text("ignored notes")
    (source / ".orchestratorignore").write_text("**/notes/private.md\n")

    files, report = rag.scan_directory(str(source))

    assert [item.path for item in files] == [safe_file.resolve()]
    assert report.indexed_files == 1
    assert report.indexed_chunks == 1
    assert report.skipped["sensitive filename"] == 1
    assert report.skipped["Ansible Vault ciphertext"] == 1
    assert report.skipped["orchestratorignore"] == 1


def test_rebuild_stores_only_safe_repo_scoped_chunks(tmp_path, monkeypatch):
    source = tmp_path / "workspace"
    repo = source / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    safe_file = repo / "main.py"
    safe_file.write_text("def indexed_function():\n    return 'safe'\n")
    (repo / "passwords.yml").write_text("excluded: true")

    monkeypatch.setattr(rag, "_INDEX_PATH", tmp_path / "chroma")
    monkeypatch.setattr(
        rag,
        "_embed",
        lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
    )
    monkeypatch.setattr(rag, "_rerank", lambda query, documents: list(range(len(documents))))

    report = rag.index_directory(str(source), rebuild=True)
    context = rag.retrieve_context("indexed function", repo_root=str(repo))

    assert report.indexed_files == 1
    assert report.indexed_chunks == 1
    assert report.uploaded_chunks == 1
    assert "indexed_function" in context
    assert str(safe_file) in context
    assert "excluded: true" not in context

    resumed = rag.index_directory(str(source), rebuild=False)
    assert resumed.indexed_chunks == 1
    assert resumed.uploaded_chunks == 0
