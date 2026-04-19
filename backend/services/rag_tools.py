import os
import time
import asyncio
import chromadb
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

CHROMA_PATH = os.getenv(
    "CHROMA_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chroma"),
)
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "ubldigital-data")

_chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = _chroma_client.get_or_create_collection(
    name=COLLECTION_NAME,
    metadata={"hnsw:space": "cosine"},
)

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
    _client = OpenAI()
    resp = _client.embeddings.create(
        input=query, model=_EMBED_MODEL, dimensions=_EMBED_DIMS
    )
    vector = resp.data[0].embedding
    if len(_embedding_cache) >= MAX_CACHE_SIZE:
        oldest_key = next(iter(_embedding_cache))
        del _embedding_cache[oldest_key]
    _embedding_cache[cache_key] = vector
    return vector


def _chroma_query(vector: list[float], top_k: int):
    res = collection.query(
        query_embeddings=[vector],
        n_results=top_k,
        include=["metadatas", "documents", "distances"],
    )
    ids = (res.get("ids") or [[]])[0]
    metadatas = (res.get("metadatas") or [[]])[0]
    documents = (res.get("documents") or [[]])[0]
    distances = (res.get("distances") or [[]])[0]

    matches = []
    for i in range(len(ids)):
        dist = distances[i] if i < len(distances) else 1.0
        score = 1.0 - float(dist)
        md = metadatas[i] if i < len(metadatas) else {}
        if md is None:
            md = {}
        if documents and i < len(documents) and documents[i] and not md.get("text"):
            md = {**md, "text": documents[i]}
        matches.append({"id": ids[i], "score": score, "metadata": md})
    return matches


async def prewarm_vector_index():
    try:
        start = time.time()
        await asyncio.to_thread(_chroma_query, [0.0] * _EMBED_DIMS, 1)
        print(f"✅ Pre-warmed Chroma HNSW index in {(time.time() - start) * 1000:.0f}ms")
    except Exception as e:
        print(f"⚠️ Vector index pre-warm failed (non-fatal): {e}")


async def prewarm_embeddings():
    try:
        resp = await _openai_async.embeddings.create(
            input=COMMON_QUERIES, model=_EMBED_MODEL, dimensions=_EMBED_DIMS
        )
        for text, item in zip(COMMON_QUERIES, resp.data):
            _embedding_cache[text.strip().lower()] = item.embedding
        print(f"✅ Pre-warmed {len(COMMON_QUERIES)} embedding cache entries")
    except Exception as e:
        print(f"⚠️ Embedding pre-warm failed (non-fatal): {e}")


def retrieve_context(query: str, top_k: int = 3, min_score: float = 0.35) -> str:
    try:
        query_vector = _sync_embed(query)
        matches = _chroma_query(query_vector, top_k)

        relevant_matches = [m for m in matches if m["score"] >= min_score]
        if not relevant_matches:
            return ""

        context_chunks = []
        seen_content = set()

        for match in relevant_matches:
            metadata = match["metadata"] or {}
            text_content = metadata.get("text", "")
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

        matches = await asyncio.to_thread(_chroma_query, vector, top_k)
        elapsed = time.time() - start_time
        print(f"🔍 RAG SEARCH completed in {elapsed:.2f}s (embed: {embed_ms:.0f}ms)")

        if not matches:
            return {"context": ""}

        relevant_matches = [m for m in matches if m["score"] >= min_score]

        if not relevant_matches:
            print(f"⚠️ RAG: All {len(matches)} results below threshold ({min_score})")
            return {"context": ""}

        context_chunks = []
        total_chars = 0
        MAX_TOTAL_CHARS = 400
        seen_content = set()

        for match in relevant_matches:
            if total_chars >= MAX_TOTAL_CHARS:
                break
            metadata = match["metadata"] or {}
            text_content = metadata.get("text", "")

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
