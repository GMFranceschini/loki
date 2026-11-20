"""Compact an abstract down to its findings.

This is extractive, not generative: it picks the most informative sentences
already in the abstract and stitches them back together in their original
order. Nothing is invented and nothing is paraphrased, which matters for
scientific text — a wrong paraphrase of a result is worse than no summary.

Two strategies:

structured   Most biomedical abstracts are labelled (BACKGROUND / METHODS /
             RESULTS / CONCLUSIONS). The conclusion and the results are the
             part worth reading, so those sections are used directly.
unstructured Otherwise sentences are ranked on how central they are to the
             abstract as a whole, how much they read like a finding rather
             than background, and where they sit.
"""

from __future__ import annotations

import re
from typing import Sequence

import numpy as np

# Sentence splitting. The lookbehind list keeps common abbreviations, initials
# and decimals from being treated as sentence ends.
_ABBREVIATIONS = (
    "e.g", "i.e", "etc", "vs", "cf", "approx", "ca", "Fig", "fig", "Figs",
    "Eq", "Ref", "refs", "No", "no", "Dr", "Prof", "St", "al", "Inc", "Ltd",
    "sp", "spp", "subsp", "var", "min", "max", "sec", "hr", "wk", "mo", "yr",
    "mg", "kg", "mL", "ml", "mM", "nM", "uM", "µM", "kDa", "bp", "kb", "Mb",
)
_ABBREV_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _ABBREVIATIONS) + r")\.",
)

# A single capital letter followed by a period, as in "J. Doe" or "E. coli".
_INITIAL_RE = re.compile(r"\b([A-Z])\.")

# A placeholder that cannot occur in real text, swapped in for a protected
# period so the splitter below does not treat it as a sentence end.
_DOT = "\x00"

_SENTENCE_END = re.compile(r'([.!?])["\')\]]*\s+(?=[A-Z0-9(\[])')


# Section labels, best first. The parser in sources/pubmed.py writes these as
# "Label: body", so they survive into the stored abstract.
_SECTION_PRIORITY = (
    ("conclusion", 5.0),
    ("interpretation", 5.0),
    ("significance", 4.5),
    ("summary", 4.0),
    ("result", 4.0),
    ("finding", 4.0),
    ("outcome", 3.0),
    ("discussion", 2.5),
    ("importance", 2.0),
    ("objective", 1.5),
    ("purpose", 1.5),
    ("aim", 1.5),
    ("background", 1.0),
    ("introduction", 1.0),
    ("method", 0.5),
    ("design", 0.5),
    ("setting", 0.3),
    ("patient", 0.3),
    ("participant", 0.3),
    ("intervention", 0.3),
    ("unlabelled", 2.0),
)

_LABEL_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)([A-Z][A-Za-z][A-Za-z /&'-]{1,38}):\s+(?=[A-Z0-9(])"
)

# Wording that signals a result rather than background.
_FINDING_CUES = (
    "we show", "we find", "we found", "we demonstrate", "we report",
    "we identify", "we identified", "we reveal", "here we", "our results",
    "our findings", "our data", "these results", "this suggests",
    "demonstrates that", "reveals that", "revealed that", "shows that",
    "showed that", "indicates that", "suggest that", "suggests that",
    "resulted in", "led to", "associated with", "compared with",
    "significantly", "conclude", "conclusion", "in contrast", "together,",
    "taken together",
)

# Wording that signals scene-setting, which a compact summary can drop.
_BACKGROUND_CUES = (
    "remains unclear", "remains unknown", "is unclear", "is unknown",
    "little is known", "poorly understood", "has been widely", "in recent years",
    "increasingly", "it is well known", "plays an important role",
    "plays a key role", "is a major", "is one of the most", "however, the",
    "despite", "to date", "growing body",
)


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, tolerating biomedical abbreviations.

    Periods that belong to abbreviations, initials and decimals are hidden
    behind a placeholder, the text is split, then the periods are restored.
    """
    text = " ".join((text or "").split())
    if not text:
        return []

    protected = _ABBREV_RE.sub(lambda m: m.group(1) + _DOT, text)
    protected = _INITIAL_RE.sub(lambda m: m.group(1) + _DOT, protected)
    # Decimal points, as in "1.5 mg".
    protected = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + _DOT + m.group(2), protected)

    marked = _SENTENCE_END.sub(lambda m: m.group(1) + "\n", protected)
    parts = (part.strip().replace(_DOT, ".") for part in marked.split("\n"))
    return [part for part in parts if part]


def parse_sections(abstract: str) -> list[tuple[str, str]]:
    """Split a labelled abstract into (label, body) pairs.

    Returns an empty list when the abstract is not labelled.
    """
    text = " ".join((abstract or "").split())
    matches = list(_LABEL_RE.finditer(text))
    if len(matches) < 2:
        # One label alone is usually a false positive, such as a gene name.
        return []

    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((match.group(1).strip().lower(), body))
    return sections


def _section_weight(label: str) -> float:
    for key, weight in _SECTION_PRIORITY:
        if key in label:
            return weight
    return 1.5  # an unrecognised label is probably content, not boilerplate


def _cue_score(sentence: str) -> float:
    lowered = sentence.lower()
    score = 0.0
    score += sum(0.6 for cue in _FINDING_CUES if cue in lowered)
    score -= sum(0.5 for cue in _BACKGROUND_CUES if cue in lowered)
    # Concrete numbers usually mean a measured result.
    if re.search(r"\d", sentence):
        score += 0.3
    return score


def _length_penalty(sentence: str) -> float:
    words = len(sentence.split())
    if words < 6:
        return -1.5          # fragments and stray headings
    if words > 60:
        return -0.8          # unreadably long in a card
    return 0.0


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Prefer to end on a sentence, then on a word.
    for boundary in (". ", "; "):
        index = cut.rfind(boundary)
        if index > max_chars * 0.5:
            return cut[: index + 1].strip()
    index = cut.rfind(" ")
    return (cut[:index] if index > 0 else cut).rstrip(" ,;:") + "…"


def _centrality(sentences: Sequence[str], embedder) -> np.ndarray:
    """How well each sentence represents the abstract as a whole."""
    if embedder is None or len(sentences) < 2:
        return np.zeros(len(sentences), dtype=np.float32)
    try:
        vectors = np.asarray(embedder.encode(list(sentences)), dtype=np.float32)
    except Exception:
        return np.zeros(len(sentences), dtype=np.float32)
    centroid = vectors.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm == 0:
        return np.zeros(len(sentences), dtype=np.float32)
    return vectors @ (centroid / norm)


def summarize(
    abstract: str | None,
    title: str | None = None,
    embedder=None,
    max_sentences: int = 3,
    max_chars: int = 420,
) -> str | None:
    """Return a compact version of the abstract, or None if there is nothing to compact."""
    text = " ".join((abstract or "").split())
    if not text:
        return None

    max_sentences = max(1, max_sentences)

    # A short abstract is already its own summary.
    if len(text) <= max_chars:
        return text

    sections = parse_sections(text)
    candidates: list[tuple[int, str, float]] = []

    if sections:
        position = 0
        for label, body in sections:
            weight = _section_weight(label)
            for sentence in split_sentences(body):
                candidates.append((position, sentence, weight + _cue_score(sentence)))
                position += 1
    else:
        sentences = split_sentences(text)
        if not sentences:
            return _truncate(text, max_chars)
        central = _centrality(sentences, embedder)
        count = len(sentences)
        for index, sentence in enumerate(sentences):
            # The last sentences of an unstructured abstract usually carry the
            # conclusion; the first usually carries the background.
            tail = index / max(1, count - 1)
            position_bonus = 1.2 * tail + (0.4 if index == 0 else 0.0)
            score = float(central[index]) * 2.0 + position_bonus + _cue_score(sentence)
            candidates.append((index, sentence, score))

    for index, (position, sentence, score) in enumerate(candidates):
        candidates[index] = (position, sentence, score + _length_penalty(sentence))

    if not candidates:
        return _truncate(text, max_chars)

    # Take the best few, then restore reading order.
    ranked = sorted(candidates, key=lambda item: item[2], reverse=True)
    chosen: list[tuple[int, str]] = []
    used = 0
    for position, sentence, _ in ranked:
        if len(chosen) >= max_sentences:
            break
        if used + len(sentence) > max_chars and chosen:
            continue
        chosen.append((position, sentence))
        used += len(sentence) + 1

    if not chosen:
        return _truncate(text, max_chars)

    chosen.sort(key=lambda item: item[0])
    summary = " ".join(sentence for _, sentence in chosen)
    summary = _truncate(summary, max_chars)

    # If compacting gained almost nothing, the abstract was already tight.
    if len(summary) >= len(text) * 0.95:
        return _truncate(text, max_chars)
    return summary
