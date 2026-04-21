"""Ingest page-text files into the local ChromaDB collection.

Run as a script: `python -m backend.utils.ingestion [--clear]`.
The OpenAI embedding model (text-embedding-3-small, 1024 dims) is
unchanged from the old Pinecone pipeline; only the vector store target
is different.
"""

import os
import glob
import uuid
from pathlib import Path

from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv

from backend.services.rag_tools import (
    CHROMA_COLLECTION,
    CHROMA_DB_PATH,
    get_collection,
)

load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

embeddings = OpenAIEmbeddings(model="text-embedding-3-small", dimensions=1024)

DEFAULT_PAGES_DIR = str(
    Path(__file__).resolve().parents[1] / "data" / "rag-data" / "pages"
)


def get_source_category(filename: str) -> dict:
    name = os.path.basename(filename).replace(".txt", "")

    if "digital" in name.lower():
        category = "Digital Banking"
        subcategory = "Digital Accounts & Services"
    elif "banking" in name.lower():
        category = "Banking Products"
        subcategory = "Accounts & Services"
    elif "ameen" in name.lower():
        category = "Islamic Banking"
        subcategory = "UBL Ameen Products"
    elif "signature" in name.lower():
        category = "Premium Banking"
        subcategory = "Signature Priority Banking"
    elif "deposit" in name.lower():
        category = "Deposits"
        subcategory = "Term Deposits & Savings"
    elif "consumer" in name.lower():
        category = "Consumer Banking"
        subcategory = "Loans & Financing"
    else:
        category = "General"
        subcategory = name.replace("_", " ").replace("-", " ").title()

    return {
        "category": category,
        "subcategory": subcategory,
        "source_file": name
    }


def _flush(collection, ids, vectors, metadatas, documents):
    if not ids:
        return
    collection.add(
        ids=ids,
        embeddings=vectors,
        metadatas=metadatas,
        documents=documents,
    )
    print(f"  ✓ Upserted batch of {len(ids)} vectors")


def ingest_text_file(file_path: str):
    print(f"📄 Ingesting {file_path}...")

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    if not text.strip():
        print(f"⚠️ Skipping empty file: {file_path}")
        return

    source_info = get_source_category(file_path)

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=150,
        length_function=len,
        is_separator_regex=False,
        separators=["\n\n", "\n", ". ", " ", ""]
    )

    chunks = text_splitter.split_text(text)
    collection = get_collection()

    ids: list[str] = []
    vectors: list[list[float]] = []
    metadatas: list[dict] = []
    documents: list[str] = []

    for i, chunk in enumerate(chunks):
        doc_id = str(uuid.uuid4())
        vector = embeddings.embed_query(chunk)

        metadata = {
            "text": chunk,
            "category": source_info["category"],
            "subcategory": source_info["subcategory"],
            "source_file": source_info["source_file"],
            "chunk_index": i,
            "total_chunks": len(chunks)
        }

        ids.append(doc_id)
        vectors.append(vector)
        metadatas.append(metadata)
        documents.append(chunk)

        if len(ids) >= 50:
            _flush(collection, ids, vectors, metadatas, documents)
            ids, vectors, metadatas, documents = [], [], [], []

    if ids:
        _flush(collection, ids, vectors, metadatas, documents)

    print(f"✅ Completed: {file_path} ({len(chunks)} chunks)")


def ingest_all_pages(pages_dir: str = DEFAULT_PAGES_DIR):
    txt_files = glob.glob(os.path.join(pages_dir, "*.txt"))

    if not txt_files:
        print(f"❌ No .txt files found in {pages_dir}")
        return

    print(
        f"\n🚀 Starting ingestion of {len(txt_files)} files into "
        f"Chroma collection '{CHROMA_COLLECTION}' at {CHROMA_DB_PATH}...\n"
    )

    for file_path in sorted(txt_files):
        try:
            ingest_text_file(file_path)
        except Exception as e:
            print(f"❌ Error processing {file_path}: {e}")

    collection = get_collection()
    count = collection.count()
    print(
        f"\n✅ Ingestion complete! Collection '{CHROMA_COLLECTION}' now has "
        f"{count} vectors."
    )


def clear_collection():
    print(f"🗑️ Clearing Chroma collection '{CHROMA_COLLECTION}'...")
    import chromadb
    from chromadb.config import Settings

    Path(CHROMA_DB_PATH).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=CHROMA_DB_PATH,
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )
    try:
        client.delete_collection(CHROMA_COLLECTION)
    except Exception as e:
        print(f"  (collection did not exist or was already empty: {e})")
    print(f"✅ Collection '{CHROMA_COLLECTION}' cleared")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        clear_collection()

    ingest_all_pages()
