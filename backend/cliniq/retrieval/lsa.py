"""NumPy-only TF-IDF + truncated SVD (no SciPy / scikit-learn).

Why: SciPy ships Fortran-compiled DLLs that Windows Smart App Control / WDAC policies can
block ("An Application Control policy has blocked this file"). NumPy's own binaries are
signed and load fine, so the LSA embedder is implemented on NumPy alone.

Semantics follow scikit-learn's TfidfVectorizer defaults that CLINIQ used:
smooth idf = ln((1+n)/(1+df)) + 1, sublinear tf = 1 + ln(tf), rows L2-normalised,
`max_features` keeps the most frequent terms; char_wb n-grams pad each word with spaces.
SVD is the randomized range finder (Halko et al.) with power iterations, like
TruncatedSVD(algorithm="randomized").
"""
from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Iterable

import numpy as np


class CSR:
    """Minimal compressed-sparse-row float32 matrix."""

    def __init__(self, data: np.ndarray, indices: np.ndarray, indptr: np.ndarray, shape: tuple[int, int]) -> None:
        self.data, self.indices, self.indptr, self.shape = data, indices, indptr, shape

    @staticmethod
    def from_rows(rows: list[dict[int, float]], ncols: int) -> CSR:
        indptr = np.zeros(len(rows) + 1, dtype=np.int64)
        for i, r in enumerate(rows):
            indptr[i + 1] = indptr[i] + len(r)
        indices = np.empty(int(indptr[-1]), dtype=np.int64)
        data = np.empty(int(indptr[-1]), dtype=np.float32)
        for i, r in enumerate(rows):
            if r:
                s = int(indptr[i])
                ks = sorted(r)
                indices[s:s + len(ks)] = ks
                data[s:s + len(ks)] = [r[k] for k in ks]
        return CSR(data, indices, indptr, (len(rows), ncols))

    @staticmethod
    def hstack(a: CSR, b: CSR) -> CSR:
        rows = []
        for i in range(a.shape[0]):
            r = {int(k): float(v) for k, v in zip(a.indices[a.indptr[i]:a.indptr[i + 1]], a.data[a.indptr[i]:a.indptr[i + 1]], strict=True)}
            for k, v in zip(b.indices[b.indptr[i]:b.indptr[i + 1]], b.data[b.indptr[i]:b.indptr[i + 1]], strict=True):
                r[int(k) + a.shape[1]] = float(v)
            rows.append(r)
        return CSR.from_rows(rows, a.shape[1] + b.shape[1])

    def transpose(self) -> CSR:
        n, d = self.shape
        row_of = np.repeat(np.arange(n, dtype=np.int64), np.diff(self.indptr))
        order = np.argsort(self.indices, kind="stable")
        counts = np.bincount(self.indices, minlength=d)
        indptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        return CSR(self.data[order], row_of[order], indptr, (d, n))

    def matmul(self, dense: np.ndarray, max_nnz: int = 200_000) -> np.ndarray:
        """self @ dense, processed in row blocks so memory stays bounded."""
        n = self.shape[0]
        out = np.zeros((n, dense.shape[1]), dtype=np.float32)
        start = 0
        while start < n:
            end = start + 1
            while end < n and self.indptr[end + 1] - self.indptr[start] <= max_nnz:
                end += 1
            lo, hi = int(self.indptr[start]), int(self.indptr[end])
            if hi > lo:
                prod = dense[self.indices[lo:hi]] * self.data[lo:hi, None]
                seg = self.indptr[start:end] - lo
                nonempty = np.diff(self.indptr[start:end + 1]) > 0
                sums = np.add.reduceat(prod, seg[nonempty], axis=0)
                out[start:end][nonempty] = sums
            start = end
        return out


def char_wb_ngrams(text: str, lo: int = 3, hi: int = 5) -> list[str]:
    grams: list[str] = []
    for w in text.lower().split():
        w = f" {w} "
        for n in range(lo, hi + 1):
            if len(w) < n:
                break
            grams.extend(w[i:i + n] for i in range(len(w) - n + 1))
    return grams


def word_ngrams(tokens: list[str], lo: int = 1, hi: int = 2) -> list[str]:
    out: list[str] = []
    for n in range(lo, hi + 1):
        out.extend(" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1))
    return out


class Tfidf:
    def __init__(self, analyzer: Callable[[str], list[str]], max_features: int | None = None) -> None:
        self.analyzer, self.max_features = analyzer, max_features

    def fit_transform(self, texts: Iterable[str]) -> CSR:
        counts = [Counter(self.analyzer(t)) for t in texts]
        total: Counter[str] = Counter()
        df: Counter[str] = Counter()
        for c in counts:
            total.update(c)
            df.update(c.keys())
        terms = sorted(total)
        if self.max_features and len(terms) > self.max_features:
            terms = sorted(sorted(terms, key=lambda t: (-total[t], t))[:self.max_features])
        self.vocab = {t: i for i, t in enumerate(terms)}
        n = len(counts)
        self.idf = np.array([math.log((1 + n) / (1 + df[t])) + 1.0 for t in terms], dtype=np.float32)
        return self._rows(counts)

    def transform(self, texts: Iterable[str]) -> CSR:
        return self._rows([Counter(self.analyzer(t)) for t in texts])

    def _rows(self, counts: list[Counter[str]]) -> CSR:
        rows = []
        for c in counts:
            r = {self.vocab[t]: (1.0 + math.log(v)) * float(self.idf[self.vocab[t]]) for t, v in c.items() if t in self.vocab}
            norm = math.sqrt(sum(v * v for v in r.values())) or 1.0
            rows.append({k: v / norm for k, v in r.items()})
        return CSR.from_rows(rows, len(self.vocab))


def randomized_svd_components(X: CSR, k: int, seed: int = 0, n_iter: int = 5, oversample: int = 10) -> np.ndarray:
    """Return the top-k right singular vectors (k x d), like TruncatedSVD.components_."""
    rng = np.random.default_rng(seed)
    XT = X.transpose()
    ell = min(k + oversample, min(X.shape))
    Q = X.matmul(rng.standard_normal((X.shape[1], ell)).astype(np.float32))
    Q, _ = np.linalg.qr(Q)
    for _ in range(n_iter):
        Z, _ = np.linalg.qr(XT.matmul(Q.astype(np.float32)))
        Q, _ = np.linalg.qr(X.matmul(Z.astype(np.float32)))
    B = XT.matmul(Q.astype(np.float32)).T  # (ell x d) == Q^T X
    _, _, Vt = np.linalg.svd(B, full_matrices=False)
    Vt = Vt[:k]
    # deterministic sign (as sklearn's svd_flip on V): largest |value| in each row positive
    signs = np.sign(Vt[np.arange(Vt.shape[0]), np.argmax(np.abs(Vt), axis=1)])
    signs[signs == 0] = 1
    return (Vt * signs[:, None]).astype(np.float32)


def sample_texts(texts: list[str], cap: int, seed: int) -> list[str]:
    if len(texts) <= cap:
        return texts
    rng = random.Random(seed)
    return [texts[i] for i in sorted(rng.sample(range(len(texts)), cap))]
