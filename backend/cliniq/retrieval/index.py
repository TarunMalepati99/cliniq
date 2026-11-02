"""Chunking, indexing and hybrid retrieval.

Lexical:  BM25 (Okapi) over contextualized chunk text (title + section + passage).
Semantic: by default a *local* LSA embedder (TF-IDF word+char n-grams → truncated SVD),
          which needs no model download. It is a modest semantic model and is named as
          such; set CLINIQ_EMBEDDER=sentence-transformers to use a neural embedder if that
          package and model are available (not exercised in CI).
Fusion:   reciprocal rank fusion (k=60) over the top-N of each retriever.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from psycopg.types.json import Jsonb
from rank_bm25 import BM25Okapi
from scipy.sparse import hstack
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize as l2

from ..db import connect
from .corpus import load_corpus

INDEX_VERSION = "hybrid-bm25-lsa-rrf-1.0"
STOP = set(["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with", "patients", "patient", "who", "must", "study", "trial", "criteria"])


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for raw in re.findall(r"[a-z0-9][a-z0-9\-/^+.]*", text.lower()):
        raw = raw.strip(".-/")
        parts = [p for p in re.split(r"[-/^]", raw) if p]
        for t in parts + ([raw.replace("-", "").replace("/", "")] if len(parts) > 1 else []):
            if t and t not in STOP:
                toks.append(t)
    return toks


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    ordinal: int
    section: str
    text: str
    char_start: int
    char_end: int
    title: str
    doc_type: str


def chunk_document(doc: dict[str, Any], max_words: int = 70) -> list[Chunk]:
    """Section-aware chunking; bullet lists are grouped, paragraphs split at ~max_words."""
    body = doc["body"]
    chunks: list[Chunk] = []
    section = "Body"
    buf: list[tuple[int, int]] = []

    def flush() -> None:
        if not buf:
            return
        s, e = buf[0][0], buf[-1][1]
        text = body[s:e].strip()
        if text:
            chunks.append(Chunk(f"{doc['doc_id']}#{len(chunks)}", doc["doc_id"], len(chunks), section, text, s, e, doc["title"], doc["doc_type"]))
        buf.clear()

    pos = 0
    for line in body.splitlines(keepends=True):
        start, end = pos, pos + len(line)
        pos = end
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            section = stripped.lstrip("# ").strip() if stripped.startswith("##") else "Body"
            continue
        if not stripped or stripped.startswith(("SYNTHETIC DOCUMENT", "AUTHORED REFERENCE NOTE")):
            if not stripped:
                flush()
            continue
        words = sum(len(body[a:b].split()) for a, b in buf)
        if buf and words + len(stripped.split()) > max_words:
            flush()
        buf.append((start, end))
    flush()
    return chunks


class Embedder(Protocol):
    name: str

    def fit(self, texts: list[str]) -> None: ...
    def encode(self, texts: list[str]) -> np.ndarray: ...


class LSAEmbedder:
    name = "lsa-tfidf-word+char-svd"

    def __init__(self, dims: int = 128, seed: int = 0) -> None:
        self.dims, self.seed = dims, seed

    def fit(self, texts: list[str]) -> None:
        self.word = TfidfVectorizer(tokenizer=tokenize, lowercase=False, ngram_range=(1, 2), sublinear_tf=True, token_pattern=None)
        self.char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=1)
        X = hstack([self.word.fit_transform(texts), self.char.fit_transform(texts)]).tocsr()
        self.svd = TruncatedSVD(n_components=min(self.dims, X.shape[0] - 1, X.shape[1] - 1), random_state=self.seed)
        self.svd.fit(X)

    def encode(self, texts: list[str]) -> np.ndarray:
        X = hstack([self.word.transform(texts), self.char.transform(texts)]).tocsr()
        return l2(self.svd.transform(X))


class STEmbedder:  # pragma: no cover - optional, requires model download
    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model)
        self.name = f"st:{model}"

    def fit(self, texts: list[str]) -> None:
        pass

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, normalize_embeddings=True))


@dataclass
class Hit:
    chunk: Chunk
    score: float
    bm25: float
    dense: float
    rank_bm25: int | None
    rank_dense: int | None

    def as_dict(self) -> dict[str, Any]:
        c = self.chunk
        return {"chunk_id": c.chunk_id, "doc_id": c.doc_id, "title": c.title, "doc_type": c.doc_type, "section": c.section,
                "passage": c.text, "char_start": c.char_start, "char_end": c.char_end, "score": round(self.score, 5),
                "bm25": round(self.bm25, 4), "dense": round(self.dense, 4), "rank_bm25": self.rank_bm25, "rank_dense": self.rank_dense}


class HybridIndex:
    def __init__(self, chunks: list[Chunk], embedder: Embedder | None = None) -> None:
        if not chunks:
            raise ValueError("cannot build an index over an empty corpus")
        self.chunks = chunks
        ctx = [f"{c.title}. {c.section}. {c.text}" for c in chunks]
        self.tokens = [tokenize(t) for t in ctx]
        self.bm25 = BM25Okapi(self.tokens)
        self.embedder = embedder or _default_embedder()
        self.embedder.fit(ctx)
        self.vecs = self.embedder.encode(ctx)
        self.corpus_sha = hashlib.sha256("".join(c.chunk_id + c.text for c in chunks).encode()).hexdigest()[:16]

    def search(self, query: str, k: int = 5, mode: str = "hybrid", pool: int = 30) -> list[Hit]:
        q = tokenize(query)
        bm = self.bm25.get_scores(q) if q else np.zeros(len(self.chunks))
        dn = self.vecs @ self.embedder.encode([query])[0]
        rb = {int(i): r for r, i in enumerate(np.argsort(-bm)[:pool]) if bm[i] > 0}
        rd = {int(i): r for r, i in enumerate(np.argsort(-dn)[:pool])}
        if mode == "bm25":
            fused = {i: float(bm[i]) for i in rb}
        elif mode == "dense":
            fused = {i: float(dn[i]) for i in rd}
        elif mode == "hybrid":
            fused = {}
            for ranks in (rb, rd):
                for i, r in ranks.items():
                    fused[i] = fused.get(i, 0.0) + 1.0 / (60 + r + 1)
        else:
            raise ValueError(mode)
        order = sorted(fused, key=lambda i: (-fused[i], self.chunks[i].chunk_id))[:k]
        return [Hit(self.chunks[i], fused[i], float(bm[i]), float(dn[i]), rb.get(i), rd.get(i)) for i in order]


def _default_embedder() -> Embedder:
    if os.environ.get("CLINIQ_EMBEDDER") == "sentence-transformers":  # pragma: no cover
        return STEmbedder()
    return LSAEmbedder()


# ---------------------------------------------------------------- persistence + singleton
_lock = threading.Lock()
_index: HybridIndex | None = None


def index_corpus(corpus_dir: Path, triggered_by: str = "cli") -> dict[str, Any]:
    """Load corpus files into rag.documents/rag.chunks (content-hash idempotent) and rebuild the index."""
    t0 = time.monotonic()
    run_id = str(uuid.uuid4())
    docs = load_corpus(corpus_dir)
    added = unchanged = 0
    with connect() as conn:
        conn.execute("INSERT INTO ops.pipeline_runs (run_id, pipeline, status, triggered_by, params) VALUES (%s,'rag_index','running',%s,%s)",
                     (run_id, triggered_by, Jsonb({"corpus_dir": str(corpus_dir)})))
        existing = {r["doc_id"]: r["content_sha256"] for r in conn.execute("SELECT doc_id, content_sha256 FROM rag.documents")}
        for d in docs:
            sha = hashlib.sha256(d["body"].encode()).hexdigest()
            if existing.get(d["doc_id"]) == sha:
                unchanged += 1
                continue
            conn.execute("DELETE FROM rag.documents WHERE doc_id=%s", (d["doc_id"],))
            conn.execute("INSERT INTO rag.documents (doc_id, title, doc_type, source, license, content_sha256, body) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (d["doc_id"], d["title"], d["doc_type"], d["source"], d["license"], sha, d["body"]))
            for c in chunk_document(d):
                conn.execute("INSERT INTO rag.chunks (chunk_id, doc_id, ordinal, section, text, char_start, char_end) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                             (c.chunk_id, c.doc_id, c.ordinal, c.section, c.text, c.char_start, c.char_end))
            added += 1
        n_chunks = conn.execute("SELECT count(*) AS n FROM rag.chunks").fetchone()["n"]  # type: ignore[index]
        metrics = {"documents_total": len(docs), "documents_added_or_updated": added, "documents_unchanged": unchanged, "chunks": n_chunks}
        conn.execute("UPDATE ops.pipeline_runs SET status='succeeded', finished_at=now(), duration_ms=%s, metrics=%s WHERE run_id=%s",
                     (int((time.monotonic() - t0) * 1000), Jsonb(metrics), run_id))
    reload_index()
    return {"run_id": run_id, "status": "succeeded", "metrics": metrics}


def load_chunks_from_db() -> list[Chunk]:
    with connect() as c:
        rows = c.execute("""SELECT k.chunk_id, k.doc_id, k.ordinal, k.section, k.text, k.char_start, k.char_end, d.title, d.doc_type
                            FROM rag.chunks k JOIN rag.documents d USING (doc_id) ORDER BY k.doc_id, k.ordinal""").fetchall()
    return [Chunk(**r) for r in rows]


def reload_index() -> HybridIndex | None:
    global _index
    with _lock:
        chunks = load_chunks_from_db()
        _index = HybridIndex(chunks) if chunks else None
        return _index


def get_index() -> HybridIndex | None:
    if _index is None:
        return reload_index()
    return _index
