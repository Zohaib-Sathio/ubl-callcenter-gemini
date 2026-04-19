import os
import glob
import uuid
import chromadb
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv

load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CHROMA_PATH = os.getenv(
    "CHROMA_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chroma"),
)
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "ubldigital-data")

client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = client.get_or_create_collection(
    name=COLLECTION_NAME,
    metadata={"hnsw:space": "cosine"},
)

embeddings = OpenAIEmbeddings(model="text-embedding-3-small", dimensions=1024)


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
        "source_file": name,
    }


def _flush(ids, vectors, metadatas, documents):
    if not ids:
        return
    collection.upsert(
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
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    chunks = text_splitter.split_text(text)

    ids, vectors, metadatas, documents = [], [], [], []
    for i, chunk in enumerate(chunks):
        doc_id = str(uuid.uuid4())
        vector = embeddings.embed_query(chunk)

        metadata = {
            "text": chunk,
            "category": source_info["category"],
            "subcategory": source_info["subcategory"],
            "source_file": source_info["source_file"],
            "chunk_index": i,
            "total_chunks": len(chunks),
        }

        ids.append(doc_id)
        vectors.append(vector)
        metadatas.append(metadata)
        documents.append(chunk)

        if len(ids) >= 50:
            _flush(ids, vectors, metadatas, documents)
            ids, vectors, metadatas, documents = [], [], [], []

    _flush(ids, vectors, metadatas, documents)

    print(f"✅ Completed: {file_path} ({len(chunks)} chunks)")


def ingest_all_pages(pages_dir: str = "pages"):
    txt_files = glob.glob(os.path.join(pages_dir, "*.txt"))

    if not txt_files:
        print(f"❌ No .txt files found in {pages_dir}")
        return

    print(f"\n🚀 Starting ingestion of {len(txt_files)} files into collection '{COLLECTION_NAME}' at {CHROMA_PATH}...\n")

    for file_path in sorted(txt_files):
        try:
            ingest_text_file(file_path)
        except Exception as e:
            print(f"❌ Error processing {file_path}: {e}")

    print(f"\n✅ Ingestion complete! All files indexed in collection '{COLLECTION_NAME}'")

    count = collection.count()
    print(f"📊 Total vectors in collection: {count}")


def clear_collection():
    print(f"🗑️ Clearing collection '{COLLECTION_NAME}'...")
    client.delete_collection(name=COLLECTION_NAME)
    global collection
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"✅ Collection '{COLLECTION_NAME}' cleared")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        clear_collection()

    ingest_all_pages("pages")
