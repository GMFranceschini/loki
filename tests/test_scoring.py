"""Scoring: impact compression, relevance discrimination, combination."""

import numpy as np
import pytest

from loki.config import Config, Scoring, Topic
from loki.scoring.embed import HashingEmbedder, get_embedder
from loki.scoring.impact import JournalIndex, impact_score, normalise_title
from loki.scoring.score import Scorer, paper_text


# ---------------------------------------------------------------- impact


def test_impact_is_monotonic():
    scores = [impact_score(jif, is_preprint=False)[0] for jif in (1, 5, 10, 30, 60)]
    assert scores == sorted(scores), "higher impact factors must score higher"


def test_impact_compresses_the_high_end():
    """The same absolute gap counts for more at the bottom of the range.

    This is the claim the UI makes: IF 2 to IF 10 should matter more than
    IF 50 to IF 90.
    """

    def at(jif):
        return impact_score(jif, is_preprint=False, cap=100.0)[0]

    assert (at(10) - at(2)) > (at(90) - at(50))


def test_impact_caps_at_one():
    score, used = impact_score(500.0, is_preprint=False, cap=60.0)
    assert score == pytest.approx(1.0)
    assert used == 500.0


def test_preprints_get_the_flat_score_and_no_impact_factor():
    score, used = impact_score(None, is_preprint=True, preprint_impact=0.33)
    assert score == pytest.approx(0.33)
    assert used is None


def test_unknown_journal_scores_below_a_known_weak_one():
    unknown, _ = impact_score(None, is_preprint=False, unknown_impact=0.20)
    known, _ = impact_score(2.0, is_preprint=False)
    assert unknown < known


# ---------------------------------------------------------------- journals


@pytest.fixture
def index():
    return JournalIndex(
        [
            {"title": "Nature", "abbrev": "Nature", "impact_factor": 50.5, "issn": ["0028-0836"]},
            {"title": "The Lancet", "abbrev": "Lancet", "impact_factor": 98.4},
            {"title": "Cell Reports", "abbrev": "Cell Rep", "impact_factor": 7.5},
        ]
    )


@pytest.mark.parametrize(
    "query",
    ["Nature", "nature", "NATURE", " Nature ", "Nature."],
)
def test_title_matching_ignores_case_and_punctuation(index, query):
    assert index.lookup(query).impact_factor == 50.5


def test_leading_the_is_ignored(index):
    assert index.lookup("Lancet").impact_factor == 98.4
    assert index.lookup("The Lancet").impact_factor == 98.4


def test_abbreviation_and_issn_match(index):
    assert index.lookup("Cell Rep").impact_factor == 7.5
    assert index.lookup(issn="0028-0836").impact_factor == 50.5
    assert index.lookup(issn="00280836").impact_factor == 50.5


def test_unknown_journal_returns_none(index):
    assert index.lookup("Journal of Imaginary Studies") is None


def test_normalise_title_strips_punctuation():
    assert normalise_title("Nature Struct. & Mol. Biology!") == "nature struct mol biology"


def test_bundled_journal_table_loads():
    loaded = JournalIndex.load()
    assert len(loaded) > 100
    assert loaded.lookup("Nature") is not None


# ---------------------------------------------------------------- embedders


def test_hashing_embedder_returns_unit_vectors():
    embedder = HashingEmbedder(dim=512)
    vectors = embedder.encode(["chromatin accessibility", "protein design"])
    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_hashing_embedder_handles_empty_text():
    vectors = HashingEmbedder(dim=128).encode(["", "   "])
    assert vectors.shape == (2, 128)
    assert np.isfinite(vectors).all()


def test_unknown_backend_falls_back_to_hashing():
    assert get_embedder("not-a-backend").name.startswith("hashing")


# ---------------------------------------------------------------- scorer


@pytest.fixture
def config():
    return Config(
        topics=(
            Topic(
                name="Genome editing",
                weight=1.0,
                prose="CRISPR base editing and prime editing of human cells.",
                pubmed="CRISPR[tiab]",
            ),
            Topic(
                name="Protein design",
                weight=1.0,
                prose="De novo protein design and structure prediction with deep learning.",
                pubmed="",
            ),
        ),
        scoring=Scoring(),
    )


@pytest.fixture
def scorer(config):
    # The hashing embedder keeps the test offline and deterministic.
    return Scorer(config, embedder=HashingEmbedder(dim=2048), journals=JournalIndex([]))


def test_paper_text_repeats_the_title():
    text = paper_text("Short title", "Long abstract")
    assert text.count("Short title") == 2


def test_relevance_is_bounded(scorer):
    relevance, _, _, _ = scorer.relevance(["CRISPR prime editing", "unrelated text", ""])
    assert all(0.0 <= float(value) <= 1.0 for value in relevance)


def test_on_topic_beats_off_topic(scorer):
    relevance, topics, _, _ = scorer.relevance(
        [
            "CRISPR base editing corrects a mutation in human cells",
            "Sediment transport in alpine river catchments",
        ]
    )
    assert relevance[0] > relevance[1]
    assert topics[0] == "Genome editing"


def test_combine_respects_the_configured_weights(config):
    scorer = Scorer(config, embedder=HashingEmbedder(dim=256), journals=JournalIndex([]))
    # Default weights are 0.7 relevance / 0.3 impact.
    assert scorer.combine(1.0, 0.0) == pytest.approx(0.7)
    assert scorer.combine(0.0, 1.0) == pytest.approx(0.3)
    assert scorer.combine(1.0, 1.0) == pytest.approx(1.0)


def test_score_records_attaches_every_field(scorer):
    records = [
        {
            "title": "Prime editing in stem cells",
            "abstract": "We edit human cells.",
            "journal": "Nature",
            "issn": "0028-0836",
            "is_preprint": False,
        },
        {
            "title": "A preprint about protein design",
            "abstract": "We design binders.",
            "journal": None,
            "issn": None,
            "is_preprint": True,
        },
    ]
    scorer.score_records(records)
    for record in records:
        for key in ("relevance", "impact", "score", "top_topic", "topic_scores", "scored_with"):
            assert key in record
        assert 0.0 <= record["score"] <= 1.0
    assert records[1]["impact_factor"] is None


def test_background_centring_suppresses_generic_similarity(config):
    embedder = HashingEmbedder(dim=2048)
    texts = ["CRISPR editing of cells", "protein design of binders", "cells and cells"]
    background = embedder.encode(texts).mean(axis=0)

    plain = Scorer(config, embedder=embedder, journals=JournalIndex([]))
    centred = Scorer(config, embedder=embedder, journals=JournalIndex([]), background=background)

    assert plain.background is None
    assert centred.background is not None
    # Centring must not break the ordering that matters.
    relevance, _, _, _ = centred.relevance(["CRISPR prime editing of human cells", "river sediment"])
    assert relevance[0] >= relevance[1]


def test_background_with_wrong_dimension_is_ignored(config):
    scorer = Scorer(
        config,
        embedder=HashingEmbedder(dim=256),
        journals=JournalIndex([]),
        background=np.ones(7, dtype=np.float32),
    )
    assert scorer.background is None
