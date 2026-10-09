"""Contract retrieval.

Each contract is split into clauses (blank-line separated paragraphs), because a
clause is the unit people cite: "contract_S004.txt#2 says Wroclaw covers door
harness only". Chunk ids look like `contract_S004.txt#2`.

Two retrievers share one interface:
- ChromaRetriever: embeddings + vector search (the real RAG path)
- KeywordRetriever: BM25, no downloads, used offline and in tests
`get_retriever()` picks Chroma when it is installed, unless SDA_RETRIEVER says otherwise.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from agent import config


@dataclass
class Chunk:
    chunk_id: str
    supplier_id: str
    text: str


def load_chunks() -> list[Chunk]:
    chunks = []
    for path in sorted(config.CONTRACTS_DIR.glob("contract_*.txt")):
        supplier_id = path.stem.split("_")[1]
        paras = [p.strip() for p in path.read_text().split("\n\n") if p.strip()]
        header = paras[0].splitlines()[0] if paras else ""
        for i, para in enumerate(paras):
            # carry the agreement title into every chunk so a clause is never orphaned
            text = para if i == 0 else f"{header} | {para}"
            chunks.append(Chunk(f"{path.name}#{i}", supplier_id, text))
    return chunks


class Retriever(Protocol):
    name: str

    def search(self, query: str, k: int = 4, supplier_id: str | None = None) -> list[dict]: ...


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class KeywordRetriever:
    name = "keyword-bm25"

    def __init__(self, chunks: list[Chunk] | None = None) -> None:
        self.chunks = chunks or load_chunks()
        self.docs = [_tokens(c.text) for c in self.chunks]
        self.avgdl = sum(len(d) for d in self.docs) / len(self.docs)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query, k=4, supplier_id=None):
        q = _tokens(query)
        scored = []
        for chunk, doc in zip(self.chunks, self.docs):
            if supplier_id and chunk.supplier_id != supplier_id:
                continue
            tf = Counter(doc)
            score = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    score += self.idf.get(t, 0) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(doc) / self.avgdl))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: -x[0])
        return [{"chunk_id": c.chunk_id, "supplier_id": c.supplier_id, "score": round(s, 3), "text": c.text}
                for s, c in scored[:k]]


class ChromaRetriever:
    name = "chroma-embeddings"

    def __init__(self) -> None:
        import chromadb

        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        self.col = client.get_or_create_collection("contracts", metadata={"hnsw:space": "cosine"})
        chunks = load_chunks()
        if self.col.count() != len(chunks):  # (re)index when contracts change
            existing = self.col.get()["ids"]
            if existing:
                self.col.delete(ids=existing)
            self.col.add(
                ids=[c.chunk_id for c in chunks],
                documents=[c.text for c in chunks],
                metadatas=[{"supplier_id": c.supplier_id} for c in chunks],
            )

    def search(self, query, k=4, supplier_id=None):
        where = {"supplier_id": supplier_id} if supplier_id else None
        res = self.col.query(query_texts=[query], n_results=k, where=where)
        out = []
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
            out.append({"chunk_id": cid, "supplier_id": meta["supplier_id"], "score": round(1 - dist, 3), "text": doc})
        return out


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    choice = config.RETRIEVER
    if choice in ("auto", "chroma"):
        try:
            return ChromaRetriever()
        except Exception as e:  # not installed, or embedding model could not download
            if choice == "chroma":
                raise
            import sys
            print(f"[rag] Chroma unavailable ({type(e).__name__}); using keyword retriever", file=sys.stderr)
    return KeywordRetriever()
