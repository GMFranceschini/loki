"""Journal prestige lookup, backed by config/journals.json.

Impact factors are not available from any free API, so Loki ships a curated
table. Matching is tolerant: full title, ISO abbreviation and ISSN all work,
and punctuation, case and a leading "The" are ignored.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from ..paths import JOURNALS_FILE

log = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalise_title(title: str | None) -> str:
    if not title:
        return ""
    text = title.strip().lower()
    text = _PUNCT.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    for prefix in ("the ", "journal of the "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def normalise_issn(issn: str | None) -> str:
    if not issn:
        return ""
    return re.sub(r"[^0-9xX]", "", issn).upper()


@dataclass(frozen=True)
class JournalHit:
    title: str
    impact_factor: float
    matched_on: str


class JournalIndex:
    """Looks up an impact factor from a journal title, abbreviation or ISSN."""

    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self._by_title: dict[str, JournalHit] = {}
        self._by_issn: dict[str, JournalHit] = {}
        for entry in entries or []:
            self._add(entry)

    def _add(self, entry: dict[str, Any]) -> None:
        title = str(entry.get("title") or "").strip()
        try:
            jif = float(entry.get("impact_factor"))
        except (TypeError, ValueError):
            return
        if not title or jif < 0:
            return

        for key, source in ((title, "title"), (entry.get("abbrev"), "abbrev")):
            norm = normalise_title(key if isinstance(key, str) else None)
            if norm and norm not in self._by_title:
                self._by_title[norm] = JournalHit(title, jif, source)

        raw_issn = entry.get("issn")
        issns = raw_issn if isinstance(raw_issn, list) else [raw_issn]
        for issn in issns:
            norm = normalise_issn(issn if isinstance(issn, str) else None)
            if norm and norm not in self._by_issn:
                self._by_issn[norm] = JournalHit(title, jif, "issn")

    def __len__(self) -> int:
        return len({hit.title for hit in self._by_title.values()})

    @classmethod
    def load(cls, path=None) -> "JournalIndex":
        path = path or JOURNALS_FILE
        if not path.exists():
            log.warning("No journal table at %s; every journal scores as unknown.", path)
            return cls([])
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.error("%s is not valid JSON (%s); every journal scores as unknown.", path, exc)
            return cls([])
        entries = doc.get("journals") if isinstance(doc, dict) else doc
        if not isinstance(entries, list):
            log.error("%s has no 'journals' list.", path)
            return cls([])
        return cls(entries)

    def lookup(
        self, journal: str | None = None, issn: str | None = None, abbrev: str | None = None
    ) -> JournalHit | None:
        for candidate in (journal, abbrev):
            hit = self._by_title.get(normalise_title(candidate))
            if hit:
                return hit
        norm_issn = normalise_issn(issn)
        if norm_issn and (hit := self._by_issn.get(norm_issn)):
            return hit
        return None


def impact_score(
    impact_factor: float | None,
    *,
    is_preprint: bool,
    cap: float = 60.0,
    preprint_impact: float = 0.33,
    unknown_impact: float = 0.20,
) -> tuple[float, float | None]:
    """Map a raw impact factor onto 0-1.

    Log compression keeps the top of the range from swamping everything else:
    the gap between IF 2 and IF 10 matters more than between IF 50 and IF 90.

    Returns (score, impact_factor_used).
    """
    if is_preprint:
        return _clamp(preprint_impact), None
    if impact_factor is None or impact_factor <= 0:
        return _clamp(unknown_impact), None
    cap = max(cap, 1.0)
    score = math.log1p(min(impact_factor, cap)) / math.log1p(cap)
    return _clamp(score), float(impact_factor)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))
