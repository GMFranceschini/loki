"""The "generic biology" background vector.

Any two life-science abstracts are fairly similar to each other: they share
vocabulary, structure and hedging. Raw cosine similarity therefore scores a
paper about knee surgery almost as high as one about prime editing, when both
are compared against genome-editing topic prose.

Subtracting the mean embedding of a large sample of recent literature removes
that shared direction. What remains is topic-specific signal. In testing this
cut an off-topic seismology abstract from 0.28 to 0.09 while on-topic papers
held near 0.45.

The vector is seeded from config/background.npy and then kept up to date in the
database as the corpus grows.
"""

from __future__ import annotations

import base64
import logging

import numpy as np

from ..paths import CONFIG_DIR

log = logging.getLogger(__name__)

SEED_FILE = CONFIG_DIR / "background.npy"

META_VECTOR = "background_vector"
META_COUNT = "background_count"
META_EMBEDDER = "background_embedder"

# Below this many papers the mean is too noisy to be worth subtracting.
MIN_SAMPLE = 250
# Recompute once the corpus has grown by this factor since the last estimate.
REGROW_FACTOR = 1.5
# Cap on how many abstracts are embedded for one estimate.
MAX_SAMPLE = 6000


def encode_vector(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def decode_vector(payload: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(payload), dtype=np.float32)


def load_seed(dim: int) -> np.ndarray | None:
    """Load the bundled background vector, if it matches the embedder."""
    if not SEED_FILE.exists():
        return None
    try:
        vec = np.load(SEED_FILE).astype(np.float32).ravel()
    except Exception as exc:
        log.warning("Could not read %s: %s", SEED_FILE, exc)
        return None
    if vec.shape[0] != dim:
        log.info(
            "Bundled background vector has dim %d but the embedder produces %d; ignoring it.",
            vec.shape[0],
            dim,
        )
        return None
    return vec


def load(conn, dim: int, embedder_name: str) -> tuple[np.ndarray | None, int]:
    """Return (background vector, sample count) for this embedder."""
    from .. import db

    stored = db.get_meta(conn, META_VECTOR)
    owner = db.get_meta(conn, META_EMBEDDER)
    if stored and owner == embedder_name:
        try:
            vec = decode_vector(stored)
            if vec.shape[0] == dim:
                count = int(db.get_meta(conn, META_COUNT, "0") or 0)
                return vec, count
            log.info("Stored background vector has the wrong dimension; recomputing.")
        except Exception as exc:
            log.warning("Stored background vector is unreadable (%s); recomputing.", exc)

    seed = load_seed(dim)
    if seed is not None:
        log.info("Using the bundled background vector from %s", SEED_FILE.name)
        return seed, MIN_SAMPLE
    return None, 0


def save(conn, vec: np.ndarray, count: int, embedder_name: str) -> None:
    from .. import db

    db.set_meta(conn, META_VECTOR, encode_vector(vec))
    db.set_meta(conn, META_COUNT, str(int(count)))
    db.set_meta(conn, META_EMBEDDER, embedder_name)


def compute(conn, embedder) -> tuple[np.ndarray | None, int]:
    """Estimate the background from a sample of stored papers."""
    from .score import paper_text

    total = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    if total < MIN_SAMPLE:
        log.info(
            "Only %d papers stored; need %d before estimating a background vector.",
            total,
            MIN_SAMPLE,
        )
        return None, 0

    # A deterministic spread across the corpus, not just the newest papers.
    rows = conn.execute(
        "SELECT title, abstract FROM papers ORDER BY id LIMIT ? OFFSET ?",
        (MAX_SAMPLE, max(0, (total - MAX_SAMPLE) // 2)),
    ).fetchall()

    texts = [paper_text(r["title"], r["abstract"]) for r in rows]
    if not texts:
        return None, 0

    vectors = embedder.encode(texts)
    mean = np.asarray(vectors, dtype=np.float32).mean(axis=0)
    log.info("Estimated a background vector from %d papers", len(texts))
    return mean, len(texts)


def should_refresh(current_count: int, corpus_count: int, has_vector: bool) -> bool:
    if corpus_count < MIN_SAMPLE:
        return False
    if not has_vector:
        return True
    return corpus_count >= max(MIN_SAMPLE, current_count * REGROW_FACTOR)
