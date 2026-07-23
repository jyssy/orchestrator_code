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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import chromadb
import httpx
import litellm
from dotenv import load_dotenv

from orchestrator.security import (
    INDEXABLE_EXTENSIONS,
    excluded_directory,
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


@dataclass
class IndexFile:
    path: Path
    repo_root: Path
    text: str


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
    response = litellm.embedding(
        model="openai/Qwen3-Embedding-8B",
        input=texts,
        api_base=_BASE_URL,
        api_key=_API_KEY,
    )
    return [item["embedding"] for item in response.data]


def _rerank(query: str, documents: list[str]) -> list[int]:
    """Return candidate indices ordered by relevance, with a stable fallback."""
    try:
        response = httpx.post(
            f"{_BASE_URL}/rerank",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            json={
                "model": "Qwen3-Reranker-8B",
                "query": query,
                "documents": documents,
                "top_n": _FINAL_K,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return [
            item["index"]
            for item in sorted(
                results,
                key=lambda item: item["relevance_score"],
                reverse=True,
            )
        ]
    except Exception:
        return list(range(min(_FINAL_K, len(documents))))


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
                repo_root=_nearest_repo_root(resolved, source, root_cache),
                text=text,
            )
        )

    report.indexed_files = len(safe_files)
    report.indexed_chunks = sum(
        (len(item.text) + _CHUNK_SIZE - 1) // _CHUNK_SIZE for item in safe_files
    )
    return safe_files, report


def retrieve_context(query: str, repo_root: str | None = None) -> str:
    """Retrieve source-labelled context, optionally restricted to one repository."""
    if not _INDEX_PATH.exists():
        return ""

    try:
        client = chromadb.PersistentClient(path=str(_INDEX_PATH))
        collection = client.get_collection(_COLLECTION_NAME)
        if collection.count() == 0:
            return ""

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
            return ""

        ranked_indices = _rerank(query, documents)
        sections: list[str] = []
        for index in ranked_indices[:_FINAL_K]:
            source = metadatas[index].get("source", "(unknown source)")
            offset = metadatas[index].get("offset", 0)
            sections.append(
                f"### Retrieved context: {source} (offset {offset})\n"
                f"{documents[index]}"
            )
        return "\n\n---\n\n".join(sections)
    except Exception:
        return ""


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

    _INDEX_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(_INDEX_PATH))

    if rebuild:
        try:
            client.delete_collection(_COLLECTION_NAME)
        except Exception:
            pass
    collection = client.get_or_create_collection(_COLLECTION_NAME)
    existing_ids = (
        set()
        if rebuild
        else set(collection.get(include=[])["ids"])
    )

    chunks: list[str] = []
    ids: list[str] = []
    metadatas: list[dict] = []
    source_root = str(Path(source_dir).expanduser().resolve())

    for item in files:
        for offset in range(0, len(item.text), _CHUNK_SIZE):
            chunk = item.text[offset : offset + _CHUNK_SIZE]
            chunk_id = hashlib.sha256(f"{item.path}:{offset}".encode()).hexdigest()
            if chunk_id not in existing_ids:
                chunks.append(chunk)
                ids.append(chunk_id)
                metadatas.append(
                    {
                        "source": str(item.path),
                        "source_root": source_root,
                        "repo_root": str(item.repo_root),
                        "offset": offset,
                    }
                )

    total = len(chunks)
    report.uploaded_chunks = total
    for start in range(0, total, _EMBED_BATCH_SIZE):
        batch_chunks = chunks[start : start + _EMBED_BATCH_SIZE]
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
