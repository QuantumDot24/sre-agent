"""
indexer.py — RAG context builder.

1. Clones (or reuses) the Medusa e-commerce repository.
2. Indexes source files into ChromaDB using sentence-transformers/MiniLM.
3. Exposes query_codebase() for the triage pipeline to fetch relevant snippets.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

REPO_URL = os.getenv("ECOMMERCE_REPO_URL", "https://github.com/medusajs/medusa.git")
REPO_DIR = os.getenv("ECOMMERCE_REPO_DIR", "./data/medusa")
CHROMA_DIR = os.getenv("CHROMA_DIR", "./data/chroma")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")

# File extensions to index
INCLUDE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".md", ".json"}
# Directories to skip inside the repo
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".next"}

MAX_CHUNK_CHARS = 800  # characters per chunk
TOP_K = 5  # results to return per query

_collection = None


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

def _clone_or_update_repo() -> Path:
    repo_path = Path(REPO_DIR)
    if repo_path.exists() and (repo_path / ".git").exists():
        logger.info(f"indexer.repo_exists: {repo_path}")
        return repo_path

    logger.info(f"indexer.cloning: url={REPO_URL}, dest={repo_path}")
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth=1", REPO_URL, str(repo_path)], check=True, capture_output=True, )
    logger.info("indexer.clone_done")
    return repo_path


def _iter_source_files(repo_path: Path):
    for p in repo_path.rglob("*"):
        if p.is_file() and p.suffix in INCLUDE_EXTS:
            if not any(skip in p.parts for skip in SKIP_DIRS):
                yield p


def _chunk_text(text: str, path: str) -> List[dict]:
    """Split file text into overlapping chunks with metadata."""
    chunks = []
    step = MAX_CHUNK_CHARS - 100  # 100-char overlap
    for i, start in enumerate(range(0, len(text), step)):
        chunk = text[start: start + MAX_CHUNK_CHARS]
        if chunk.strip():
            chunks.append({"id": f"{path}::chunk{i}", "text": chunk, "meta": {"file": path, "chunk": i}, })
    return chunks


# ---------------------------------------------------------------------------
# ChromaDB + embeddings
# ---------------------------------------------------------------------------

def _get_collection():
    global _collection
    if _collection is not None:
        return _collection

    import chromadb
    from chromadb.utils import embedding_functions

    Path(CHROMA_DIR).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)

    _collection = client.get_or_create_collection(name="medusa_codebase", embedding_function=ef,
        metadata={"hnsw:space": "cosine"}, )
    logger.info(f"indexer.collection_ready: count={_collection.count()}")
    return _collection


def build_index(force_rebuild: bool = False) -> None:
    """
    Clone repo if needed and build/refresh the ChromaDB index.
    Safe to call multiple times (idempotent unless force_rebuild=True).
    """
    col = _get_collection()
    if col.count() > 0 and not force_rebuild:
        logger.info(f"indexer.already_indexed: docs={col.count()}")
        return

    repo_path = _clone_or_update_repo()

    all_ids, all_docs, all_metas = [], [], []
    file_count = 0

    for fpath in _iter_source_files(repo_path):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel = str(fpath.relative_to(repo_path))
        for chunk in _chunk_text(text, rel):
            all_ids.append(chunk["id"])
            all_docs.append(chunk["text"])
            all_metas.append(chunk["meta"])
        file_count += 1

        # Upsert in batches of 500 to avoid memory spikes
        if len(all_ids) >= 500:
            col.upsert(ids=all_ids, documents=all_docs, metadatas=all_metas)
            all_ids, all_docs, all_metas = [], [], []

    if all_ids:
        col.upsert(ids=all_ids, documents=all_docs, metadatas=all_metas)

    logger.info(f"indexer.build_complete: files={file_count}, total_chunks={col.count()}")


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------

def query_codebase(query: str, top_k: int = TOP_K) -> str:
    """
    Return a formatted string of the most relevant code/doc snippets
    for the given incident query. Safe to call if index is not built yet
    (returns empty string with a warning).
    """
    try:
        col = _get_collection()
        if col.count() == 0:
            logger.warning("indexer.empty_index")
            return ""

        results = col.query(query_texts=[query], n_results=min(top_k, col.count()))
        docs = results["documents"][0]
        metas = results["metadatas"][0]

        parts = []
        for doc, meta in zip(docs, metas):
            parts.append(f"[{meta['file']} chunk {meta['chunk']}]\n{doc}")

        combined = "\n\n---\n\n".join(parts)
        logger.info(f"indexer.query: query={query[:60]}, results={len(docs)}")
        return combined

    except Exception as e:
        logger.error(f"indexer.query_error: {e}")
        return ""
