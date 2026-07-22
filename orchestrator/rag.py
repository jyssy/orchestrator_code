"""
rag.py — embed query with Qwen3-Embedding-8B (REALMS), search local
ChromaDB index, rerank with Qwen3-Reranker-8B (REALMS).
"""

import os
import httpx
import litellm
import chromadb
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

_BASE_URL = os.getenv("REALMS_BASE_URL", "https://reallms.rescloud.iu.edu/direct/v1")
_API_KEY = os.getenv("REALMS_API_KEY", "")
_INDEX_PATH = Path(os.getenv("RAG_INDEX_PATH", "~/.orchestrator/chroma")).expanduser()
_TOP_K = 8       # candidates before reranking
_FINAL_K = 3     # chunks returned after reranking


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts via REALMS Qwen3-Embedding-8B using litellm (consistent auth with chat)."""
    response = litellm.embedding(
        model="openai/Qwen3-Embedding-8B",
        input=texts,
        api_base=_BASE_URL,
        api_key=_API_KEY,
    )
    return [item["embedding"] for item in response.data]


def _rerank(query: str, documents: list[str]) -> list[int]:
    """
    Rerank documents via REALMS Qwen3-Reranker-8B.
    Returns indices sorted by relevance (best first).
    Falls back to original order if endpoint unavailable.
    """
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
        return [r["index"] for r in sorted(results, key=lambda x: x["relevance_score"], reverse=True)]
    except Exception:
        return list(range(min(_FINAL_K, len(documents))))


def retrieve_context(query: str) -> str:
    """
    Retrieve relevant code/doc chunks for a query.
    Returns a single string of concatenated top chunks.
    Returns empty string if index doesn't exist yet.
    """
    if not _INDEX_PATH.exists():
        return ""

    try:
        client = chromadb.PersistentClient(path=str(_INDEX_PATH))
        collection = client.get_collection("codebase")

        query_embedding = _embed([query])[0]
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(_TOP_K, collection.count()),
        )

        docs = results["documents"][0]
        if not docs:
            return ""

        try:
            ranked_indices = _rerank(query, docs)
            top_docs = [docs[i] for i in ranked_indices[:_FINAL_K]]
        except Exception:
            top_docs = docs[:_FINAL_K]

        return "\n\n---\n\n".join(top_docs)

    except Exception:
        return ""


def index_directory(source_dir: str) -> int:
    """
    Embed and index all text/code files in source_dir into ChromaDB.
    Returns the number of chunks indexed.
    """
    import hashlib

    source = Path(source_dir).expanduser()
    _INDEX_PATH.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(_INDEX_PATH))
    collection = client.get_or_create_collection("codebase")

    extensions = {".py", ".yml", ".yaml", ".tf", ".md", ".sh", ".j2", ".toml", ".cfg", ".ini", ".txt"}
    files = [f for f in source.rglob("*") if f.suffix in extensions and f.is_file()]

    chunk_size = 1500
    chunks, ids, metadatas = [], [], []

    for file in files:
        try:
            text = file.read_text(errors="ignore")
        except Exception:
            continue
        for i in range(0, len(text), chunk_size):
            chunk = text[i : i + chunk_size]
            chunk_id = hashlib.md5(f"{file}:{i}".encode()).hexdigest()
            chunks.append(chunk)
            ids.append(chunk_id)
            metadatas.append({"source": str(file), "offset": i})

    # Embed in batches of 8 (smaller = more reliable for remote API)
    batch_size = 8
    for start in range(0, len(chunks), batch_size):
        batch_chunks = chunks[start : start + batch_size]
        batch_ids = ids[start : start + batch_size]
        batch_meta = metadatas[start : start + batch_size]
        embeddings = _embed(batch_chunks)
        collection.upsert(documents=batch_chunks, embeddings=embeddings, ids=batch_ids, metadatas=batch_meta)

    return len(chunks)
