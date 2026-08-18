from pathlib import Path

import orchestrator.rag as rag
from orchestrator.results import ComponentResult, ResultStatus
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
    (source / ".git").mkdir(parents=True)
    (source / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "remote-approved"\n'
    )
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


def test_scan_does_not_read_repository_content_without_remote_opt_in(
    tmp_path, monkeypatch
):
    source = tmp_path / "repo"
    source.mkdir()
    (source / ".git").mkdir()
    candidate = source / "main.py"
    candidate.write_text("ordinary code")

    original_read_bytes = Path.read_bytes

    def guarded_read(path):
        if path == candidate:
            raise AssertionError("local-only content was read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    files, report = rag.scan_directory(str(source))

    assert files == []
    assert report.skipped["model egress local-only"] == 1


def test_rebuild_stores_only_safe_repo_scoped_chunks(tmp_path, monkeypatch):
    source = tmp_path / "workspace"
    repo = source / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "remote-approved"\n'
    )
    safe_file = repo / "main.py"
    safe_file.write_text("def indexed_function():\n    return 'safe'\n")
    (repo / "passwords.yml").write_text("excluded: true")

    monkeypatch.setattr(rag, "_INDEX_PATH", tmp_path / "chroma")
    monkeypatch.setattr(
        rag,
        "_embed",
        lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
    )
    monkeypatch.setattr(
        rag,
        "_rerank_result",
        lambda query, documents: ComponentResult(
            "reranker", ResultStatus.SUCCESS, list(range(len(documents)))
        ),
    )
    monkeypatch.setattr(rag, "guard_text", lambda text, **kwargs: None)

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


def test_refresh_updates_only_the_scanned_repository(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo_a = workspace / "repo-a"
    repo_b = workspace / "repo-b"
    for repo in (repo_a, repo_b):
        (repo / ".git").mkdir(parents=True)
        (repo / ".orchestrator-policy.toml").write_text(
            '[model_egress]\nclassification = "remote-approved"\n'
        )
    file_a = repo_a / "main.py"
    file_a.write_text("def original_a():\n    return 'a1'\n")
    file_b = repo_b / "main.py"
    file_b.write_text("def original_b():\n    return 'b1'\n")

    monkeypatch.setattr(rag, "_INDEX_PATH", tmp_path / "chroma")
    monkeypatch.setattr(rag, "_embed", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    monkeypatch.setattr(
        rag,
        "_rerank_result",
        lambda query, documents: ComponentResult(
            "reranker", ResultStatus.SUCCESS, list(range(len(documents)))
        ),
    )
    monkeypatch.setattr(rag, "guard_text", lambda text, **kwargs: None)

    # Full rebuild across the whole workspace, indexing both repos.
    rag.index_directory(str(workspace), rebuild=True)
    assert "original_a" in rag.retrieve_context("original", repo_root=str(repo_a))
    assert "original_b" in rag.retrieve_context("original", repo_root=str(repo_b))

    # Edit repo_a only, then refresh scoped to repo_a alone.
    file_a.write_text("def edited_a():\n    return 'a2'\n")
    report = rag.refresh_repositories(str(repo_a))

    assert report.indexed_files == 1
    assert report.uploaded_chunks == 1

    # repo_a reflects the edit; repo_b is untouched by the repo_a-scoped refresh.
    context_a = rag.retrieve_context("edited", repo_root=str(repo_a))
    assert "edited_a" in context_a
    assert "original_a" not in context_a
    assert "original_b" in rag.retrieve_context("original", repo_root=str(repo_b))


def test_refresh_removes_chunks_for_files_no_longer_present(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".orchestrator-policy.toml").write_text(
        '[model_egress]\nclassification = "remote-approved"\n'
    )
    stale_file = repo / "stale.py"
    stale_file.write_text("def stale_marker():\n    return True\n")

    monkeypatch.setattr(rag, "_INDEX_PATH", tmp_path / "chroma")
    monkeypatch.setattr(rag, "_embed", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    monkeypatch.setattr(
        rag,
        "_rerank_result",
        lambda query, documents: ComponentResult(
            "reranker", ResultStatus.SUCCESS, list(range(len(documents)))
        ),
    )
    monkeypatch.setattr(rag, "guard_text", lambda text, **kwargs: None)

    rag.index_directory(str(repo), rebuild=True)
    assert "stale_marker" in rag.retrieve_context("stale", repo_root=str(repo))

    stale_file.unlink()
    (repo / "kept.py").write_text("def kept_marker():\n    return True\n")
    rag.refresh_repositories(str(repo))

    context = rag.retrieve_context("marker", repo_root=str(repo))
    assert "kept_marker" in context
    assert "stale_marker" not in context


def test_retrieval_rejects_legacy_chunks_without_current_policy_metadata(
    tmp_path, monkeypatch
):
    index_path = tmp_path / "chroma"
    monkeypatch.setattr(rag, "_INDEX_PATH", index_path)
    monkeypatch.setattr(rag, "_embed", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(
        rag,
        "_rerank",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale documents reached reranking")
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    client = rag.chromadb.PersistentClient(path=str(index_path))
    collection = client.get_or_create_collection(rag._COLLECTION_NAME)
    collection.add(
        documents=["legacy unsafe candidate"],
        embeddings=[[1.0, 0.0]],
        ids=["legacy"],
        metadatas=[
            {"source": str(repo / "old.py"), "repo_root": str(repo), "offset": 0}
        ],
    )

    assert rag.retrieve_context("candidate", repo_root=str(repo)) == ""
