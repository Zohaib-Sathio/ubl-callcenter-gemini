"""Local ChromaDB-backed RAG tools.

Replaces the previous Pinecone integration with a local, persistent
ChromaDB store. Embeddings are still produced by OpenAI
(`text-embedding-3-small`, 1024 dims) and passed to Chroma explicitly,
so the semantic quality is unchanged — only the vector store moved
from a remote managed index to a local on-disk one.

Startup warmup (`prewarm_embeddings`) both pre-computes embeddings for
common caller queries and runs a throwaway Chroma query, so the first
real `search_knowledge_base` call does not pay HNSW load cost.
"""

import os
import time
import asyncio
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

CHROMA_DB_PATH = os.getenv(
    "CHROMA_DB_PATH",
    str(Path(__file__).resolve().parents[1] / "data" / "chroma"),
)
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "ubldigital_data")

_openai_async = AsyncOpenAI()
_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIMS = 1024

_embedding_cache: dict[str, list[float]] = {}
MAX_CACHE_SIZE = 200

COMMON_QUERIES = [
    "digital account", "card activation", "debit card", "account opening",
    "UBL Digital App", "fund transfer", "bill payment", "loan",
    "balance inquiry", "CNIC verification", "TPIN", "card PIN",
    "remittance", "cheque book", "ATM", "branch",
    "Islamic banking", "Ameen account", "credit card", "VISA",
    "mobile account", "Asaan account", "Netbanking", "UBL Pay",
]


def _get_client() -> chromadb.api.ClientAPI:
    Path(CHROMA_DB_PATH).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=CHROMA_DB_PATH,
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )


_client: Optional[chromadb.api.ClientAPI] = None
_collection = None


def get_collection():
    """Return the Chroma collection, creating client/collection on first use."""
    global _client, _collection
    if _collection is not None:
        return _collection
    if _client is None:
        _client = _get_client()
    _collection = _client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


async def _async_embed(query: str) -> list[float]:
    cache_key = query.strip().lower()
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]

    resp = await _openai_async.embeddings.create(
        input=query, model=_EMBED_MODEL, dimensions=_EMBED_DIMS
    )
    vector = resp.data[0].embedding

    if len(_embedding_cache) >= MAX_CACHE_SIZE:
        oldest_key = next(iter(_embedding_cache))
        del _embedding_cache[oldest_key]

    _embedding_cache[cache_key] = vector
    return vector


def _sync_embed(query: str) -> list[float]:
    cache_key = query.strip().lower()
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]
    from openai import OpenAI
    _client_sync = OpenAI()
    resp = _client_sync.embeddings.create(
        input=query, model=_EMBED_MODEL, dimensions=_EMBED_DIMS
    )
    vector = resp.data[0].embedding
    if len(_embedding_cache) >= MAX_CACHE_SIZE:
        oldest_key = next(iter(_embedding_cache))
        del _embedding_cache[oldest_key]
    _embedding_cache[cache_key] = vector
    return vector


def _chroma_query(vector: list[float], top_k: int) -> dict:
    collection = get_collection()
    return collection.query(
        query_embeddings=[vector],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )


def _iter_matches(results: dict):
    """Flatten Chroma's list-of-lists query result into (text, metadata, score)
    tuples, with score = 1 - cosine_distance so higher is better (matches the
    min_score semantics the callers were written against)."""
    ids_batch = results.get("ids") or [[]]
    docs_batch = results.get("documents") or [[]]
    metas_batch = results.get("metadatas") or [[]]
    dists_batch = results.get("distances") or [[]]
    if not ids_batch or not ids_batch[0]:
        return
    docs = docs_batch[0] if docs_batch else []
    metas = metas_batch[0] if metas_batch else []
    dists = dists_batch[0] if dists_batch else []
    for i in range(len(ids_batch[0])):
        doc = docs[i] if i < len(docs) else ""
        meta = metas[i] if i < len(metas) else {}
        dist = dists[i] if i < len(dists) else 1.0
        score = 1.0 - float(dist)
        yield doc or (meta or {}).get("text", ""), (meta or {}), score


async def prewarm_embeddings():
    """Pre-compute OpenAI embeddings for common caller queries and warm up
    the Chroma HNSW index with a throwaway query, so the first real
    search_knowledge_base call does not pay cold-start cost."""
    try:
        resp = await _openai_async.embeddings.create(
            input=COMMON_QUERIES, model=_EMBED_MODEL, dimensions=_EMBED_DIMS
        )
        warm_vector: Optional[list[float]] = None
        for text, item in zip(COMMON_QUERIES, resp.data):
            _embedding_cache[text.strip().lower()] = item.embedding
            if warm_vector is None:
                warm_vector = item.embedding
        print(f"✅ Pre-warmed {len(COMMON_QUERIES)} embedding cache entries")

        t0 = time.perf_counter()
        collection = await asyncio.to_thread(get_collection)
        count = await asyncio.to_thread(collection.count)
        if warm_vector is not None and count > 0:
            await asyncio.to_thread(_chroma_query, warm_vector, 1)
        warm_ms = (time.perf_counter() - t0) * 1000.0
        print(
            f"🔥 [RAG] Chroma collection '{CHROMA_COLLECTION}' ready "
            f"(docs={count}, warmup_ms={warm_ms:.0f}, path={CHROMA_DB_PATH})"
        )
    except Exception as e:
        print(f"⚠️ Embedding pre-warm failed (non-fatal): {e}")


def retrieve_context(query: str, top_k: int = 3, min_score: float = 0.35) -> str:
    try:
        query_vector = _sync_embed(query)
        results = _chroma_query(query_vector, top_k)

        context_chunks = []
        seen_content = set()

        for text_content, metadata, score in _iter_matches(results):
            if score < min_score:
                continue
            category = metadata.get("category", "General")
            subcategory = metadata.get("subcategory", "")

            content_hash = hash(text_content[:100])
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)

            if text_content:
                context_chunks.append(
                    f"[{category} - {subcategory}]\n{text_content}"
                )

        return "\n\n---\n\n".join(context_chunks) if context_chunks else ""

    except Exception as e:
        print(f"Error retrieving context: {e}")
        return ""


async def search_knowledge_base(query: str, top_k: int = 3, min_score: float = 0.35) -> dict:
    try:
        start_time = time.time()
        print(f"\n🔍 RAG SEARCH: '{query}'")

        vector = await _async_embed(query)
        embed_ms = (time.time() - start_time) * 1000

        results = await asyncio.to_thread(_chroma_query, vector, top_k)
        elapsed = time.time() - start_time
        print(f"🔍 RAG SEARCH completed in {elapsed:.2f}s (embed: {embed_ms:.0f}ms)")

        matches = list(_iter_matches(results))
        if not matches:
            return {"context": ""}

        relevant_matches = [m for m in matches if m[2] >= min_score]

        if not relevant_matches:
            print(f"⚠️ RAG: All {len(matches)} results below threshold ({min_score})")
            return {"context": ""}

        context_chunks = []
        total_chars = 0
        MAX_TOTAL_CHARS = 400
        seen_content = set()

        for text_content, _metadata, _score in relevant_matches:
            if total_chars >= MAX_TOTAL_CHARS:
                break

            content_hash = hash(text_content[:100])
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)

            if text_content:
                remaining = MAX_TOTAL_CHARS - total_chars
                text = text_content[:min(250, remaining)]
                total_chars += len(text)
                context_chunks.append(text)

        combined_context = "\n".join(context_chunks)

        print(f"✅ RAG: Found {len(context_chunks)} results ({total_chars} chars) in {elapsed:.2f}s")

        return {"context": combined_context}

    except Exception as e:
        print(f"⚠️ search_knowledge_base error: {str(e)}")
        return {"context": ""}
