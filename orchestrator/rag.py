"""
Safe, repository-scoped RAG indexing and retrieval.

File contents are sent to REALMS for embedding and reranking. The scanner must
therefore exclude generated, ignored, and secret-bearing files before reading
or transmitting them.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import chromadb
from dotenv import load_dotenv

from orchestrator.egress_guard import ModelEgressBlocked, egress_scope, guard_text
from orchestrator.model_gateway import ProviderFailure, embedding, rerank
from orchestrator.results import ComponentResult, ResultStatus, diagnostic
from orchestrator.security import (
    INDEXABLE_EXTENSIONS,
    MODEL_EGRESS_POLICY_FILENAME,
    DataClassification,
    excluded_directory,
    load_data_classification,
    load_ignore_patterns,
    matches_ignore_patterns,
    sensitive_content_reason,
    sensitive_path_reason,
)

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

_BASE_URL = os.getenv("REALMS_BASE_URL", "https://reallms.rescloud.iu.edu/direct/v1")
_API_KEY = os.getenv("REALMS_API_KEY", "")
_INDEX_PATH = Path(os.getenv("RAG_INDEX_PATH", "~/.orchestrator/chroma")).expanduser()
_COLLECTION_NAME = "codebase"
_TOP_K = 8
_FINAL_K = 3
_CHUNK_SIZE = 1500
_MAX_FILE_BYTES = 2_000_000
_EMBED_BATCH_SIZE = int(os.getenv("RAG_EMBED_BATCH_SIZE", "32"))
_EGRESS_POLICY_VERSION = 1


@dataclass
class IndexFile:
    path: Path
    repo_root: Path
    text: str
    classification: DataClassification


@dataclass
class IndexReport:
    source: str
    discovered_files: int = 0
    indexed_files: int = 0
    indexed_chunks: int = 0
    uploaded_chunks: int = 0
    skipped: Counter = field(default_factory=Counter)
    rebuilt: bool = False

    def summary(self, *, audit: bool = False) -> str:
        skipped = ", ".join(
            f"{reason}={count}" for reason, count in sorted(self.skipped.items())
        ) or "none"
        if audit:
            result = (
                f"Would index {self.indexed_chunks} chunks from "
                f"{self.indexed_files} files under {self.source}."
            )
        else:
            action = "Rebuilt" if self.rebuilt else "Updated"
            result = (
                f"{action} index with {self.indexed_chunks} chunks from "
                f"{self.indexed_files} files under {self.source}; "
                f"stored {self.uploaded_chunks} new chunks."
            )
        return f"{result} Skipped: {skipped}"


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed safe texts via REALMS Qwen3-Embedding-8B."""
    response = embedding(
        model="openai/Qwen3-Embedding-8B",
        texts=texts,
        api_base=_BASE_URL,
        api_key=_API_KEY,
    )
    return [item["embedding"] for item in response.data]


def _rerank_result(query: str, documents: list[str]) -> ComponentResult[list[int]]:
    """Return ranked indices and make any stable-order fallback visible."""
    try:
        response = rerank(
            base_url=_BASE_URL,
            api_key=_API_KEY,
            model="Qwen3-Reranker-8B",
            query=query,
            documents=documents,
            top_n=_FINAL_K,
            timeout=60.0,
        )
        results = response.json().get("results", [])
        if not isinstance(results, list) or not results:
            raise ValueError("reranker results are missing")
        indices = [
            item["index"]
            for item in sorted(
                results,
                key=lambda item: item["relevance_score"],
                reverse=True,
            )
        ]
        if any(
            not isinstance(index, int) or index < 0 or index >= len(documents)
            for index in indices
        ) or len(indices) != len(set(indices)):
            raise ValueError("reranker indices are invalid")
        return ComponentResult("reranker", ResultStatus.SUCCESS, indices)
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        fallback = list(range(min(_FINAL_K, len(documents))))
        failure_details = {
            ResultStatus.UNAVAILABLE_DEPENDENCY: (
                "reranker_unavailable_fallback",
                "Reranking was unavailable; stable retrieval order was used.",
            ),
            ResultStatus.INVALID_CONFIGURATION: (
                "reranker_invalid_configuration_fallback",
                "Reranking configuration was invalid; stable retrieval order was used.",
            ),
            ResultStatus.INVALID_INPUT: (
                "reranker_request_rejected_fallback",
                "The reranking request was rejected; stable retrieval order was used.",
            ),
            ResultStatus.INTERNAL_FAILURE: (
                "reranker_internal_fallback",
                "Reranking failed unexpectedly; stable retrieval order was used.",
            ),
        }
        code, message = failure_details.get(
            exc.status,
            (
                "reranker_fallback",
                "Reranking could not complete; stable retrieval order was used.",
            ),
        )
        return ComponentResult(
            "reranker",
            ResultStatus.DEGRADED_SUCCESS,
            fallback,
            code=code,
            message=message,
            attempts=exc.attempts,
            warnings=(diagnostic("reranker", code, message),),
        )
    except (KeyError, TypeError, ValueError):
        fallback = list(range(min(_FINAL_K, len(documents))))
        return ComponentResult(
            "reranker",
            ResultStatus.DEGRADED_SUCCESS,
            fallback,
            code="reranker_malformed_response",
            message="Reranking returned malformed data; stable retrieval order was used.",
            warnings=(
                diagnostic(
                    "reranker",
                    "reranker_malformed_response",
                    "Reranking returned malformed data; stable retrieval order was used.",
                ),
            ),
        )


def _rerank(query: str, documents: list[str]) -> list[int]:
    """Compatibility wrapper returning only candidate indices."""
    return _rerank_result(query, documents).value or []


def _nearest_repo_root(path: Path, source: Path, cache: dict[Path, Path]) -> Path:
    current = path.parent
    visited: list[Path] = []

    while True:
        if current in cache:
            root = cache[current]
            break
        visited.append(current)
        if (current / ".git").exists():
            root = current
            break
        if current == source or current.parent == current:
            root = source
            break
        current = current.parent

    for directory in visited:
        cache[directory] = root
    return root


def _git_ignored_paths(files: list[Path], source: Path) -> set[Path]:
    """Ask each nested Git repository which untracked candidates are ignored."""
    root_cache: dict[Path, Path] = {}
    grouped: dict[Path, list[Path]] = defaultdict(list)
    for path in files:
        grouped[_nearest_repo_root(path, source, root_cache)].append(path)

    ignored: set[Path] = set()
    for repo_root, repo_files in grouped.items():
        if not (repo_root / ".git").exists():
            continue

        relative_paths = [str(path.relative_to(repo_root)) for path in repo_files]
        try:
            result = subprocess.run(
                ["git", "check-ignore", "--stdin", "-z"],
                cwd=repo_root,
                input="\0".join(relative_paths) + "\0",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        for relative in result.stdout.split("\0"):
            if relative:
                ignored.add((repo_root / relative).resolve())
    return ignored


def scan_directory(source_dir: str) -> tuple[list[IndexFile], IndexReport]:
    """Return files safe to transmit plus an audit report; performs no API calls."""
    source = Path(source_dir).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Index source is not a directory: {source}")

    report = IndexReport(source=str(source))
    ignore_patterns = load_ignore_patterns(source)
    candidates: list[Path] = []

    for directory, directory_names, filenames in os.walk(source):
        directory_path = Path(directory)
        retained_directories: list[str] = []
        for name in directory_names:
            path = directory_path / name
            if path.is_symlink():
                report.skipped["symlink directories"] += 1
            elif excluded_directory(path):
                report.skipped["excluded directories"] += 1
            elif matches_ignore_patterns(path.relative_to(source), ignore_patterns):
                report.skipped["ignored directories"] += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for filename in filenames:
            path = directory_path / filename
            if path.suffix.lower() not in INDEXABLE_EXTENSIONS:
                continue
            report.discovered_files += 1

            if path.is_symlink():
                report.skipped["symlinks"] += 1
                continue
            if path.name == MODEL_EGRESS_POLICY_FILENAME:
                report.skipped["model-egress policy"] += 1
                continue
            relative = path.relative_to(source)
            if matches_ignore_patterns(relative, ignore_patterns):
                report.skipped["orchestratorignore"] += 1
                continue
            path_reason = sensitive_path_reason(path)
            if path_reason:
                report.skipped[path_reason] += 1
                continue
            candidates.append(path)

    git_ignored = _git_ignored_paths(candidates, source)
    root_cache: dict[Path, Path] = {}
    safe_files: list[IndexFile] = []

    for path in candidates:
        resolved = path.resolve()
        if resolved in git_ignored:
            report.skipped["gitignore"] += 1
            continue
        repo_root = _nearest_repo_root(resolved, source, root_cache)
        try:
            classification = load_data_classification(repo_root)
        except ValueError:
            report.skipped["invalid model-egress policy"] += 1
            continue
        if classification is not DataClassification.REMOTE_APPROVED:
            report.skipped[f"model egress {classification.value}"] += 1
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                report.skipped["too large"] += 1
                continue
            raw = path.read_bytes()
        except OSError:
            report.skipped["unreadable"] += 1
            continue
        if b"\0" in raw[:4096]:
            report.skipped["binary"] += 1
            continue

        text = raw.decode("utf-8", errors="ignore")
        content_reason = sensitive_content_reason(text)
        if content_reason:
            report.skipped[content_reason] += 1
            continue
        if not text.strip():
            report.skipped["empty"] += 1
            continue

        safe_files.append(
            IndexFile(
                path=resolved,
                repo_root=repo_root,
                text=text,
                classification=classification,
            )
        )

    report.indexed_files = len(safe_files)
    report.indexed_chunks = sum(
        (len(item.text) + _CHUNK_SIZE - 1) // _CHUNK_SIZE for item in safe_files
    )
    return safe_files, report


def retrieve_context_result(
    query: str, repo_root: str | None = None
) -> ComponentResult[str]:
    """Retrieve context with explicit no-match, stale, and unavailable states."""
    if not _INDEX_PATH.exists():
        return ComponentResult(
            "retrieval",
            ResultStatus.UNAVAILABLE_DEPENDENCY,
            code="rag_index_missing",
            message="The local RAG index is unavailable.",
        )

    try:
        client = chromadb.PersistentClient(path=str(_INDEX_PATH))
        collection = client.get_collection(_COLLECTION_NAME)
        if collection.count() == 0:
            return ComponentResult(
                "retrieval",
                ResultStatus.SUCCESS,
                "",
                code="rag_no_matches",
                message="The RAG index contains no matching context.",
            )

        query_embedding = _embed([query])[0]
        query_args = {
            "query_embeddings": [query_embedding],
            "n_results": min(_TOP_K, collection.count()),
            "include": ["documents", "metadatas"],
        }
        if repo_root:
            query_args["where"] = {
                "repo_root": str(Path(repo_root).expanduser().resolve())
            }
        results = collection.query(**query_args)

        documents = results["documents"][0]
        metadatas = results["metadatas"][0]
        if not documents:
            return ComponentResult(
                "retrieval",
                ResultStatus.SUCCESS,
                "",
                code="rag_no_matches",
                message="The RAG query returned no matching context.",
            )

        current_entries = [
            (document, metadata)
            for document, metadata in zip(documents, metadatas, strict=True)
            if metadata.get("egress_policy_version") == _EGRESS_POLICY_VERSION
        ]
        if not current_entries:
            return ComponentResult(
                "retrieval",
                ResultStatus.DEGRADED_SUCCESS,
                "",
                code="rag_index_stale",
                message="Only stale RAG chunks were found; none were used.",
                warnings=(
                    diagnostic(
                        "retrieval",
                        "rag_index_stale",
                        "Only stale RAG chunks were found; none were used.",
                    ),
                ),
            )
        documents = [entry[0] for entry in current_entries]
        metadatas = [entry[1] for entry in current_entries]

        rerank_result = _rerank_result(query, documents)
        ranked_indices = rerank_result.value or []
        sections: list[str] = []
        for index in ranked_indices[:_FINAL_K]:
            source = metadatas[index].get("source", "(unknown source)")
            offset = metadatas[index].get("offset", 0)
            sections.append(
                f"### Retrieved context: {source} (offset {offset})\n{documents[index]}"
            )
        context = "\n\n---\n\n".join(sections)
        return ComponentResult(
            "retrieval",
            rerank_result.status,
            context,
            code=rerank_result.code,
            message=rerank_result.message,
            attempts=rerank_result.attempts,
            warnings=rerank_result.warnings,
        )
    except ModelEgressBlocked:
        raise
    except ProviderFailure as exc:
        return ComponentResult(
            "retrieval",
            exc.status,
            code=exc.code,
            message=exc.safe_message,
            attempts=exc.attempts,
        )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError):
        return ComponentResult(
            "retrieval",
            ResultStatus.UNAVAILABLE_DEPENDENCY,
            code="rag_query_failed",
            message="The local RAG index could not be queried.",
        )
    except Exception:  # noqa: BLE001 - expose a safe status, never raw index errors
        return ComponentResult(
            "retrieval",
            ResultStatus.INTERNAL_FAILURE,
            code="rag_internal_failure",
            message="RAG retrieval failed unexpectedly.",
        )


def retrieve_context(query: str, repo_root: str | None = None) -> str:
    """Compatibility wrapper returning context or a sanitized explicit failure."""
    result = retrieve_context_result(query, repo_root)
    if not result.usable:
        raise ProviderFailure(
            result.status, result.code, result.message, result.attempts
        )
    return result.value or ""


def index_directory(
    source_dir: str,
    *,
    rebuild: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> IndexReport:
    """
    Scan, embed, and persist safe files.

    With rebuild=True, the existing collection is deleted before any safe chunks
    are stored, ensuring stale or previously unsafe entries cannot survive.
    """
    files, report = scan_directory(source_dir)
    report.rebuilt = rebuild

    chunks: list[str] = []
    ids: list[str] = []
    metadatas: list[dict] = []
    source_root = str(Path(source_dir).expanduser().resolve())

    for item in files:
        for offset in range(0, len(item.text), _CHUNK_SIZE):
            chunk = item.text[offset : offset + _CHUNK_SIZE]
            chunk_id = hashlib.sha256(f"{item.path}:{offset}".encode()).hexdigest()
            chunks.append(chunk)
            ids.append(chunk_id)
            metadatas.append(
                {
                    "source": str(item.path),
                    "source_root": source_root,
                    "repo_root": str(item.repo_root),
                    "offset": offset,
                    "egress_policy_version": _EGRESS_POLICY_VERSION,
                }
            )

    # Complete all mandatory local scans before mutating the persistent index.
    for chunk in chunks:
        guard_text(chunk, source="RAG index chunk")

    _INDEX_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(_INDEX_PATH))

    if rebuild:
        try:
            client.delete_collection(_COLLECTION_NAME)
        except Exception:
            pass
    collection = client.get_or_create_collection(_COLLECTION_NAME)
    existing_ids = set() if rebuild else set(collection.get(include=[])["ids"])

    if existing_ids:
        retained = [index for index, chunk_id in enumerate(ids) if chunk_id not in existing_ids]
        chunks = [chunks[index] for index in retained]
        ids = [ids[index] for index in retained]
        metadatas = [metadatas[index] for index in retained]

    total = len(chunks)
    report.uploaded_chunks = total
    for start in range(0, total, _EMBED_BATCH_SIZE):
        batch_chunks = chunks[start : start + _EMBED_BATCH_SIZE]
        batch_metadatas = metadatas[start : start + _EMBED_BATCH_SIZE]
        for repo_root in {item["repo_root"] for item in batch_metadatas}:
            if (
                load_data_classification(Path(repo_root))
                is not DataClassification.REMOTE_APPROVED
            ):
                raise ModelEgressBlocked(
                    "Repository policy changed before RAG embedding transmission"
                )
        with egress_scope(DataClassification.REMOTE_APPROVED):
            embeddings = _embed(batch_chunks)
        collection.upsert(
            documents=batch_chunks,
            embeddings=embeddings,
            ids=ids[start : start + _EMBED_BATCH_SIZE],
            metadatas=metadatas[start : start + _EMBED_BATCH_SIZE],
        )
        if progress:
            progress(min(start + _EMBED_BATCH_SIZE, total), total)

    return report
