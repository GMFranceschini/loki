"""Combine topic relevance and journal impact into one ranking score."""

from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np

from ..config import Config
from ..summarize import summarize
from .embed import Embedder, get_embedder, _l2_normalise
from .impact import JournalIndex, impact_score

log = logging.getLogger(__name__)


def paper_text(title: str | None, abstract: str | None) -> str:
    """The text compared against topic prose.

    The title is repeated because it is short, dense and the most reliable
    signal of what a paper is actually about.
    """
    title = (title or "").strip()
    abstract = (abstract or "").strip()
    return f"{title} {title} {abstract}".strip()


class Scorer:
    """Scores papers against the configured topic profile.

    relevance  cosine similarity to topic prose, squashed onto 0-1
    impact     log-compressed journal impact factor, 0-1
    score      weighted sum of the two
    """

    def __init__(
        self,
        config: Config,
        embedder: Embedder | None = None,
        journals: JournalIndex | None = None,
        background: "np.ndarray | None" = None,
    ):
        self.config = config
        self.scoring = config.scoring
        self.summary_config = config.summary
        self.embedder = embedder or get_embedder(
            config.embedding.backend, config.embedding.model
        )
        self.journals = journals if journals is not None else JournalIndex.load()
        self.fingerprint = config.fingerprint()

        self._topic_names = [t.name for t in config.topics]
        raw_weights = np.array([t.weight for t in config.topics], dtype=np.float32)
        self._topic_weights = raw_weights / raw_weights.max()

        self.background = self._validate_background(background)
        self._topic_matrix = self._build_topic_matrix(config)

        log.info(
            "Scorer ready: %d topics, embedder=%s, %d journals, background=%s",
            len(self._topic_names),
            self.embedder.name,
            len(self.journals),
            "on" if self.background is not None else "off",
        )

    def _build_topic_matrix(self, config: Config) -> np.ndarray:
        """Embed each topic as a blend of its name and its prose.

        The prose is long, so on its own it drowns out the name: a topic
        called "Hi-C" described with generic chromatin wording would match
        anything chromatin-flavoured. Blending the name back in keeps the
        label and the matching about the same thing.
        """
        names = self._project(self.embedder.encode([t.name for t in config.topics]))
        prose = self._project(self.embedder.encode([t.prose for t in config.topics]))

        alpha = max(0.0, min(1.0, self.scoring.topic_name_weight))
        return _l2_normalise(alpha * names + (1.0 - alpha) * prose)

    def _validate_background(self, background) -> "np.ndarray | None":
        if background is None:
            return None
        vec = np.asarray(background, dtype=np.float32).ravel()
        if vec.shape[0] != self.embedder.dim:
            log.warning(
                "Background vector dim %d does not match embedder dim %d; "
                "scoring without it.",
                vec.shape[0],
                self.embedder.dim,
            )
            return None
        return vec

    def _project(self, vectors: np.ndarray) -> np.ndarray:
        """Remove the generic-literature direction, then renormalise.

        Without this step every life-science abstract scores highly against
        every life-science topic, because they share so much vocabulary.
        """
        if self.background is None:
            return _l2_normalise(vectors)
        return _l2_normalise(np.asarray(vectors, dtype=np.float32) - self.background)

    # ---------------------------------------------------------------- relevance

    def _squash(self, sim: float) -> float:
        lo, hi = self.scoring.sim_floor, self.scoring.sim_ceiling
        if hi <= lo:
            return max(0.0, min(1.0, sim))
        return max(0.0, min(1.0, (sim - lo) / (hi - lo)))

    def tags_for(self, sims: np.ndarray) -> list[str]:
        """Topics that genuinely describe one paper, strongest first.

        Returns an empty list when nothing clears the bar. A paper with no
        confident tag is still ranked; it simply is not labelled, which is
        more honest than attaching the least-bad topic.
        """
        order = np.argsort(sims)[::-1]
        best = float(sims[order[0]])
        if best < self.scoring.tag_min_sim:
            return []

        floor = max(self.scoring.tag_min_sim, best * self.scoring.tag_relative)
        chosen = [self._topic_names[int(i)] for i in order if float(sims[i]) >= floor]
        return chosen[: max(1, self.scoring.max_tags)]

    def relevance(
        self, texts: Sequence[str]
    ) -> tuple[np.ndarray, list[str | None], list[dict[str, float]], list[list[str]]]:
        """Return (relevance, best topic or None, per-topic sims, tags)."""
        if not texts:
            return np.zeros(0, dtype=np.float32), [], [], []

        vecs = self._project(self.embedder.encode(list(texts)))
        # (n_papers, n_topics) cosine similarity; both sides are L2-normalised.
        sims = vecs @ self._topic_matrix.T
        weighted = sims * self._topic_weights[None, :]

        rows = np.arange(len(texts))
        # Ranking uses the weighted similarity, so topic weights steer the
        # digest. Labelling uses the raw similarity, so a lower-weighted topic
        # is still named when it is the one that actually matches.
        best_idx = np.argmax(weighted, axis=1)
        best = weighted[rows, best_idx]

        # A small bonus for matching several topics, so cross-cutting papers
        # rank above ones that hit a single topic equally well.
        share = max(0.0, min(1.0, self.scoring.best_topic_share))
        mean_others = (
            (weighted.sum(axis=1) - best) / max(1, weighted.shape[1] - 1)
            if weighted.shape[1] > 1
            else np.zeros_like(best)
        )
        blended = share * best + (1.0 - share) * np.maximum(mean_others, 0.0)

        relevance = np.array([self._squash(float(v)) for v in blended], dtype=np.float32)

        tags = [self.tags_for(sims[i]) for i in rows]
        # The headline topic is the best RAW match, and only when it is good
        # enough to be worth showing.
        top_topics: list[str | None] = [row[0] if row else None for row in tags]
        per_topic = [
            {name: round(float(sims[i, j]), 4) for j, name in enumerate(self._topic_names)}
            for i in rows
        ]
        return relevance, top_topics, per_topic, tags

    # ---------------------------------------------------------------- impact

    def impact(self, journal: str | None, issn: str | None, is_preprint: bool, abbrev: str | None = None):
        hit = None if is_preprint else self.journals.lookup(journal, issn, abbrev)
        return impact_score(
            hit.impact_factor if hit else None,
            is_preprint=is_preprint,
            cap=self.scoring.impact_factor_cap,
            preprint_impact=self.scoring.preprint_impact,
            unknown_impact=self.scoring.unknown_journal_impact,
        )

    # ---------------------------------------------------------------- combined

    # ---------------------------------------------------------------- summary

    def summarize(self, abstract: str | None, title: str | None = None) -> str | None:
        """Compact an abstract down to its findings, for the expanded card."""
        if not self.summary_config.enabled:
            return None
        try:
            return summarize(
                abstract,
                title,
                embedder=self.embedder,
                max_sentences=self.summary_config.max_sentences,
                max_chars=self.summary_config.max_chars,
            )
        except Exception:
            # A summary is a convenience: never let it fail a whole fetch.
            log.warning("Could not summarise %r", (title or "")[:60], exc_info=True)
            return None

    def combine(self, relevance: float, impact: float) -> float:
        w_rel = self.scoring.relevance_weight
        w_imp = self.scoring.impact_weight
        total = w_rel + w_imp
        if total <= 0:
            return 0.0
        return float((w_rel * relevance + w_imp * impact) / total)

    def score_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach relevance, impact and score to each record, in place."""
        if not records:
            return records

        texts = [paper_text(r.get("title"), r.get("abstract")) for r in records]
        relevance, top_topics, per_topic, tags = self.relevance(texts)

        for rec, rel, topic, sims, tag_list in zip(
            records, relevance, top_topics, per_topic, tags
        ):
            imp, jif = self.impact(
                rec.get("journal"),
                rec.get("issn"),
                bool(rec.get("is_preprint")),
                rec.get("journal_abbrev"),
            )
            rec["relevance"] = round(float(rel), 4)
            rec["impact"] = round(float(imp), 4)
            rec["impact_factor"] = jif
            rec["score"] = round(self.combine(float(rel), float(imp)), 4)
            rec["top_topic"] = topic
            rec["tags"] = tag_list
            rec["summary"] = self.summarize(rec.get("abstract"), rec.get("title"))
            rec["topic_scores"] = sims
            rec["scored_with"] = self.fingerprint
        return records
