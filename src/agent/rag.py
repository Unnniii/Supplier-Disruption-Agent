"""Contract retrieval.

Each contract is split into clauses (blank-line separated paragraphs), because a
clause is the unit people cite: "contract_S004.txt#2 says Wroclaw covers door
harness only". Chunk ids look like `contract_S004.txt#2`.

Retrievers share one interface:
- ChromaRetriever + GeminiEmbedder: free Gemini embeddings, Chroma vector search (default)
- ChromaRetriever alone: Chroma's own local embedding model
- KeywordRetriever: BM25, no downloads, used offline and in tests
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
        paras = [p.strip() for p in path.read_text(encoding="utf-8").split("\n\n") if p.strip()]
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


class GeminiEmbedder:
    """Gemini Embedding (free tier). Documents and questions are embedded with
    different task types, which is what the model is trained for."""

    name = "gemini"

    def __init__(self, client=None) -> None:
        if client is None:
            from google import genai
            client = genai.Client()
        self._client = client

    def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        from google.genai import types

        from agent.llm import _retry_on_rate_limit
        cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=768)

        def call(batch: list[str]):
            # Each text is its own Content. A bare list of strings can be read as
            # several parts of ONE input, which returns one vector for the batch.
            contents = [types.Content(parts=[types.Part(text=t)]) for t in batch]
            return _retry_on_rate_limit(lambda: self._client.models.embed_content(
                model=config.EMBED_MODEL, contents=contents, config=cfg)).embeddings

        out: list[list[float]] = []
        for i in range(0, len(texts), 50):  # batch to stay inside request limits
            batch = texts[i:i + 50]
            vectors = call(batch)
            if len(vectors) != len(batch):  # model would not batch: one request per text
                vectors = [call([t])[0] for t in batch]
            out.extend(v.values for v in vectors)
        return out

    def documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


class ChromaRetriever:
    """Vector search in Chroma. With an embedder (Gemini) the vectors are made by
    us; without one Chroma uses its default local model (downloads ~80 MB once)."""

    def __init__(self, embedder: GeminiEmbedder | None = None) -> None:
        import chromadb

        self.embedder = embedder
        self.name = f"chroma+{embedder.name}" if embedder else "chroma+default"
        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        col_name = f"contracts_{embedder.name}" if embedder else "contracts"
        self.col = client.get_or_create_collection(col_name, metadata={"hnsw:space": "cosine"})
        chunks = load_chunks()
        if self.col.count() != len(chunks):  # (re)index only when contracts change: saves free quota
            existing = self.col.get()["ids"]
            if existing:
                self.col.delete(ids=existing)
            kwargs = {}
            import sys
            print(f"[rag] indexing {len(chunks)} contract clauses (first run only)...", file=sys.stderr, flush=True)
            if embedder:
                kwargs["embeddings"] = embedder.documents([c.text for c in chunks])
            self.col.add(ids=[c.chunk_id for c in chunks], documents=[c.text for c in chunks],
                         metadatas=[{"supplier_id": c.supplier_id} for c in chunks], **kwargs)

    def search(self, query, k=4, supplier_id=None):
        where = {"supplier_id": supplier_id} if supplier_id else None
        if self.embedder:
            res = self.col.query(query_embeddings=[self.embedder.query(query)], n_results=k, where=where)
        else:
            res = self.col.query(query_texts=[query], n_results=k, where=where)
        out = []
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
            out.append({"chunk_id": cid, "supplier_id": meta["supplier_id"], "score": round(1 - dist, 3), "text": doc})
        return out


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    """auto: Gemini embeddings if a key is set, else Chroma's local model, else BM25."""
    import os
    import sys

    choice = config.RETRIEVER
    attempts = {
        "gemini": [lambda: ChromaRetriever(GeminiEmbedder())],
        "chroma": [ChromaRetriever],
        "keyword": [],
        "auto": ([lambda: ChromaRetriever(GeminiEmbedder())] if os.getenv("GEMINI_API_KEY") else []) + [ChromaRetriever],
    }[choice]
    for build in attempts:
        try:
            return build()
        except Exception as e:  # not installed, no key, or model download blocked
            if choice != "auto":
                raise
            print(f"[rag] retriever unavailable ({type(e).__name__}: {str(e)[:80]}); trying next", file=sys.stderr)
    return KeywordRetriever()
