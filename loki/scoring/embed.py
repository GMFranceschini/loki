"""Text embedding backends.

Two backends, both fully local:

model2vec   Static distilled embeddings. Real semantic similarity, CPU only,
            about 30 MB. Downloads once, then works offline.
hashing     Pure-numpy hashed bag-of-words. No download, no model. Lexical
            only, so it misses papers that use different wording than your
            topic prose. It exists so Loki always runs.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol, Sequence

import numpy as np

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-']+")

# Words too common in biology abstracts to carry topic signal.
_STOP = frozenset("""
a an the and or but if then than that this these those of in on at to for from by
with without within into over under between among during via using used use uses
is are was were be been being has have had do does did can could may might will
would should shall must not no nor so such as it its their his her our your we
they he she you i there here when where which who whom whose what how why all any
both each few more most other some only own same too very s t just also however
thus therefore here show shown showed results result study studies data method
methods conclusion conclusions background objective purpose we report reported
paper article findings suggest suggests demonstrate demonstrated observed observe
significant significantly increase increased decrease decreased high low level
levels group groups patient patients analysis analyses between associated
association compared comparison following based respectively although while
""".split())


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Return an (n, dim) array of L2-normalised row vectors."""


def _l2_normalise(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat.reshape(1, -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class Model2VecEmbedder:
    """Static sentence embeddings from the model2vec library."""

    def __init__(self, model_name: str = "minishlab/potion-base-8M"):
        from model2vec import StaticModel

        self._model = StaticModel.from_pretrained(model_name)
        self.name = f"model2vec:{model_name}"
        probe = self._model.encode(["probe"])
        self.dim = int(np.asarray(probe).reshape(1, -1).shape[1])

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        texts = [t if t and t.strip() else "empty" for t in texts]
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = self._model.encode(list(texts))
        return _l2_normalise(vecs)


class HashingEmbedder:
    """Hashed bag-of-words with sublinear term weighting. No model needed."""

    def __init__(self, dim: int = 4096):
        self.dim = dim
        self.name = f"hashing:{dim}"

    @staticmethod
    def _tokens(text: str) -> list[str]:
        words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP and len(w) > 2]
        # Add adjacent bigrams so multi-word concepts keep some identity.
        bigrams = [f"{a}_{b}" for a, b in zip(words, words[1:])]
        return words + bigrams

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            counts: dict[int, float] = {}
            for tok in self._tokens(text or ""):
                idx = self._bucket(tok)
                counts[idx] = counts.get(idx, 0.0) + 1.0
            for idx, count in counts.items():
                out[i, idx] = 1.0 + np.log(count)
        return _l2_normalise(out)


def get_embedder(backend: str = "model2vec", model: str = "minishlab/potion-base-8M") -> Embedder:
    """Build the requested backend, falling back to hashing if it is unavailable."""
    backend = (backend or "model2vec").strip().lower()

    if backend == "hashing":
        return HashingEmbedder()

    if backend == "model2vec":
        try:
            return Model2VecEmbedder(model)
        except Exception as exc:  # import error, no network on first run, bad name
            log.warning(
                "model2vec backend unavailable (%s); falling back to the hashing "
                "embedder. Relevance will be lexical, not semantic.",
                exc,
            )
            return HashingEmbedder()

    log.warning("Unknown embedding backend %r; using hashing.", backend)
    return HashingEmbedder()
