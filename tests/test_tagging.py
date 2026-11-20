"""Tag selection and the padded digest windows."""

from datetime import date

import numpy as np
import pytest

from loki.config import Config, Scoring, Topic
from loki.scoring.embed import HashingEmbedder
from loki.scoring.impact import JournalIndex
from loki.scoring.score import Scorer
from loki.pipeline import BASE_WINDOW_DAYS, windows


@pytest.fixture
def config():
    return Config(
        topics=(
            Topic("Hi-C", 1.0, "Chromosome conformation capture, contact matrices and loop calling.", ""),
            Topic("DNA methylation", 0.5, "CpG methylation, bisulfite sequencing and methylation clocks.", ""),
        ),
        scoring=Scoring(),
    )


@pytest.fixture
def scorer(config):
    return Scorer(config, embedder=HashingEmbedder(dim=2048), journals=JournalIndex([]))


# ---------------------------------------------------------------- tags


def test_no_tag_when_nothing_clears_the_floor(scorer):
    sims = np.array([0.01, 0.02], dtype=np.float32)
    assert scorer.tags_for(sims) == []


def test_single_tag_when_one_topic_dominates(scorer):
    sims = np.array([0.50, 0.10], dtype=np.float32)
    assert scorer.tags_for(sims) == ["Hi-C"]


def test_second_tag_appears_only_when_close(scorer):
    # 0.45 is 90% of 0.50, above the 0.80 relative bar.
    assert scorer.tags_for(np.array([0.50, 0.45], dtype=np.float32)) == ["Hi-C", "DNA methylation"]
    # 0.30 is 60% of 0.50, below it.
    assert scorer.tags_for(np.array([0.50, 0.30], dtype=np.float32)) == ["Hi-C"]


def test_tags_are_ordered_strongest_first(scorer):
    tags = scorer.tags_for(np.array([0.42, 0.50], dtype=np.float32))
    assert tags == ["DNA methylation", "Hi-C"]


def test_tag_count_is_capped(config):
    config = Config(
        topics=tuple(
            Topic(f"T{i}", 1.0, f"Description number {i} with enough words to pass.", "")
            for i in range(6)
        ),
        scoring=Scoring(max_tags=2),
    )
    scorer = Scorer(config, embedder=HashingEmbedder(dim=512), journals=JournalIndex([]))
    assert len(scorer.tags_for(np.full(6, 0.5, dtype=np.float32))) == 2


def test_raising_the_floor_removes_tags(config):
    strict = Config(topics=config.topics, scoring=Scoring(tag_min_sim=0.6))
    scorer = Scorer(strict, embedder=HashingEmbedder(dim=512), journals=JournalIndex([]))
    assert scorer.tags_for(np.array([0.5, 0.1], dtype=np.float32)) == []


def test_relative_of_one_gives_at_most_one_tag(config):
    single = Config(topics=config.topics, scoring=Scoring(tag_relative=1.0))
    scorer = Scorer(single, embedder=HashingEmbedder(dim=512), journals=JournalIndex([]))
    assert scorer.tags_for(np.array([0.50, 0.499], dtype=np.float32)) == ["Hi-C"]


def test_label_ignores_topic_weight(scorer):
    """A low-weighted topic is still named when it is the real match.

    Weights steer the ranking; they must not rename what a paper is about.
    """
    sims = np.array([0.20, 0.45], dtype=np.float32)  # DNA methylation wins on raw score
    assert scorer.tags_for(sims)[0] == "DNA methylation"


def test_relevance_returns_tags_and_a_matching_top_topic(scorer):
    relevance, top, sims, tags = scorer.relevance(
        ["Hi-C contact matrices and loop calling of chromosome conformation capture data"]
    )
    assert tags[0], "an on-topic paper should be tagged"
    assert top[0] == tags[0][0]


def test_off_topic_paper_is_scored_but_not_tagged(scorer):
    _, top, _, tags = scorer.relevance(["Bridge scour hydrology in alpine catchments"])
    assert tags[0] == []
    assert top[0] is None


def test_score_records_attaches_tags(scorer):
    records = [{"title": "Hi-C loop calling", "abstract": "Contact matrices.",
                "journal": None, "issn": None, "is_preprint": True}]
    scorer.score_records(records)
    assert isinstance(records[0]["tags"], list)


# ------------------------------------------------- topic name blending


def test_topic_name_contributes_to_matching():
    """A paper matching only the topic NAME should still score above nothing.

    This is the failure mode where a topic is renamed but its description is
    left describing something else.
    """
    prose = "Completely unrelated subject matter concerning soil chemistry and erosion."
    config = Config(topics=(Topic("Hi-C", 1.0, prose, ""),), scoring=Scoring(topic_name_weight=0.9))
    scorer = Scorer(config, embedder=HashingEmbedder(dim=2048), journals=JournalIndex([]))

    named, _, _, _ = scorer.relevance(["Hi-C contact map analysis"])
    unrelated, _, _, _ = scorer.relevance(["Bridge scour hydrology"])
    assert named[0] >= unrelated[0]


def test_name_weight_of_zero_uses_prose_only(config):
    prose_only = Config(topics=config.topics, scoring=Scoring(topic_name_weight=0.0))
    scorer = Scorer(prose_only, embedder=HashingEmbedder(dim=512), journals=JournalIndex([]))
    assert scorer._topic_matrix.shape == (2, 512)


# ---------------------------------------------------------------- windows


def test_padding_widens_both_sides():
    reference = date(2026, 11, 17)
    start, end = windows(reference, 10)["day"]
    assert start == date(2026, 11, 7)
    assert end == date(2026, 11, 27)


def test_zero_padding_restores_strict_windows():
    reference = date(2026, 11, 17)
    assert windows(reference, 0)["day"] == (reference, reference)
    assert windows(reference, 0)["week"][0] == date(2026, 11, 11)


def test_windows_stay_nested_with_padding():
    spans = windows(date(2026, 11, 17), 10)
    day, week, month = spans["day"], spans["week"], spans["month"]
    assert week[0] <= day[0] and week[1] >= day[1]
    assert month[0] <= week[0] and month[1] >= week[1]


def test_padded_window_lengths():
    spans = windows(date(2026, 11, 17), 10)
    for name, base in BASE_WINDOW_DAYS.items():
        start, end = spans[name]
        assert (end - start).days + 1 == base + 20


def test_negative_padding_is_treated_as_zero():
    reference = date(2026, 11, 17)
    assert windows(reference, -5)["day"] == (reference, reference)
