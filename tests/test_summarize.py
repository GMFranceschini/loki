"""Compacting abstracts: sentence splitting, section parsing, selection."""

import pytest

from loki.summarize import parse_sections, split_sentences, summarize

STRUCTURED = (
    "Background: Chromatin folding has been widely studied and its role remains unclear. "
    "Methods: We performed Hi-C on 24 samples and called loops with a standard pipeline. "
    "Results: We found that cohesin depletion removed 87% of loops within 2 hours. "
    "Conclusions: Loop extrusion by cohesin is the dominant mechanism of loop formation."
)

UNSTRUCTURED = (
    "Chromatin organisation plays an important role in gene regulation. "
    "Little is known about how loops form in living cells. "
    "To date, most studies have relied on fixed material. "
    "Here we develop a live-imaging method to track individual loops. "
    "We show that loops are short-lived and reform within minutes. "
    "Our findings establish loop extrusion as a highly dynamic process."
)


# ---------------------------------------------------------------- splitting


def test_splits_simple_sentences():
    assert len(split_sentences("One thing happened. Another thing followed.")) == 2


@pytest.mark.parametrize(
    "text",
    [
        "We dosed at 1.5 mg/kg daily thereafter.",
        "Samples were treated (e.g. with DMSO) before imaging.",
        "Smith et al. reported this previously in mice.",
        "We compared A vs. B across conditions.",
    ],
)
def test_abbreviations_and_decimals_do_not_split(text):
    assert len(split_sentences(text)) == 1


def test_abbreviation_periods_are_restored():
    out = split_sentences("We used 1.5 mg doses. Smith et al. agreed with this.")
    assert out == ["We used 1.5 mg doses.", "Smith et al. agreed with this."]


def test_empty_input_gives_no_sentences():
    assert split_sentences("") == []
    assert split_sentences(None) == []


# ---------------------------------------------------------------- sections


def test_parses_labelled_sections():
    sections = parse_sections(STRUCTURED)
    assert [label for label, _ in sections] == [
        "background", "methods", "results", "conclusions",
    ]


def test_section_bodies_are_captured():
    bodies = dict(parse_sections(STRUCTURED))
    assert "cohesin depletion" in bodies["results"]


def test_unlabelled_abstract_has_no_sections():
    assert parse_sections(UNSTRUCTURED) == []


def test_a_single_label_is_not_treated_as_structure():
    # Avoids false positives on things like a gene name followed by a colon.
    assert parse_sections("TP53: a tumour suppressor. It is mutated often.") == []


# ---------------------------------------------------------------- summaries


def test_returns_none_for_no_abstract():
    assert summarize(None) is None
    assert summarize("") is None


def test_short_abstract_is_returned_unchanged():
    text = "We found loops in cells."
    assert summarize(text) == text


def test_summary_is_shorter_than_the_abstract():
    summary = summarize(UNSTRUCTURED, max_chars=200)
    assert len(summary) < len(UNSTRUCTURED)


def test_summary_is_extractive_only():
    """Every sentence in the summary must come from the abstract verbatim."""
    summary = summarize(UNSTRUCTURED, max_chars=250)
    for sentence in split_sentences(summary):
        assert sentence.rstrip("…").strip() in UNSTRUCTURED


def test_structured_summary_prefers_findings_over_methods():
    summary = summarize(STRUCTURED, max_sentences=2, max_chars=260)
    assert "cohesin" in summary.lower()
    assert "standard pipeline" not in summary


def test_unstructured_summary_prefers_findings_over_background():
    summary = summarize(UNSTRUCTURED, max_sentences=2, max_chars=200)
    assert "plays an important role" not in summary


def test_sentence_order_is_preserved():
    summary = summarize(UNSTRUCTURED, max_sentences=3, max_chars=400)
    positions = [UNSTRUCTURED.index(s.rstrip("…").strip())
                 for s in split_sentences(summary)
                 if s.rstrip("…").strip() in UNSTRUCTURED]
    assert positions == sorted(positions)


def test_max_chars_is_respected():
    for limit in (120, 200, 300):
        assert len(summarize(UNSTRUCTURED, max_chars=limit)) <= limit


def test_max_sentences_is_respected():
    # max_chars must be under the abstract length, or the already-short
    # passthrough returns the abstract untouched.
    summary = summarize(UNSTRUCTURED, max_sentences=1, max_chars=200)
    assert len(split_sentences(summary)) == 1


def test_works_without_an_embedder():
    assert summarize(UNSTRUCTURED, max_chars=200, embedder=None)


def test_a_broken_embedder_does_not_raise():
    class Broken:
        def encode(self, texts):
            raise RuntimeError("model unavailable")

    assert summarize(UNSTRUCTURED, max_chars=200, embedder=Broken())


def test_abstract_without_sentence_breaks_is_truncated():
    text = "word " * 300
    summary = summarize(text, max_chars=150)
    assert len(summary) <= 150


def test_scorer_attaches_a_summary():
    from loki.config import Config, Scoring, SummaryConfig, Topic
    from loki.scoring.embed import HashingEmbedder
    from loki.scoring.impact import JournalIndex
    from loki.scoring.score import Scorer

    config = Config(
        topics=(Topic("Hi-C", 1.0, "Chromosome conformation capture and loop calling.", ""),),
        scoring=Scoring(),
    )
    config = Config(topics=config.topics, scoring=config.scoring,
                    summary=SummaryConfig(max_sentences=2, max_chars=200))
    scorer = Scorer(config, embedder=HashingEmbedder(dim=512), journals=JournalIndex([]))
    records = [{"title": "Loops", "abstract": STRUCTURED, "journal": None,
                "issn": None, "is_preprint": True}]
    scorer.score_records(records)
    assert records[0]["summary"]
    assert len(records[0]["summary"]) < len(STRUCTURED)


def test_summaries_can_be_turned_off():
    from loki.config import Config, Scoring, SummaryConfig, Topic
    from loki.scoring.embed import HashingEmbedder
    from loki.scoring.impact import JournalIndex
    from loki.scoring.score import Scorer

    config = Config(
        topics=(Topic("Hi-C", 1.0, "Chromosome conformation capture and loop calling.", ""),),
        scoring=Scoring(),
        summary=SummaryConfig(enabled=False),
    )
    scorer = Scorer(config, embedder=HashingEmbedder(dim=512), journals=JournalIndex([]))
    assert scorer.summarize(STRUCTURED) is None


def test_abstract_already_within_the_limit_is_left_alone():
    """Compacting something already short would only lose information."""
    assert summarize(STRUCTURED, max_chars=len(STRUCTURED) + 10) == STRUCTURED


def test_rescore_rebuilds_summaries_only_when_asked(tmp_path, monkeypatch):
    """A stored summary survives a plain rescore and is rebuilt with --summaries."""
    from datetime import date

    from loki import db
    from loki.config import Config, EmbeddingConfig, SummaryConfig, Topic
    from loki.pipeline import rescore_all

    monkeypatch.setattr(db, "DB_FILE", tmp_path / "test.db")
    config = Config(
        topics=(Topic("Hi-C", 1.0, "Chromosome conformation capture and contact maps.", ""),),
        embedding=EmbeddingConfig(backend="hashing"),
        summary=SummaryConfig(enabled=True, max_sentences=2, max_chars=200),
    )

    long_abstract = (
        "Chromatin architecture has been widely studied in recent years. "
        "We developed a new contact mapping method for single cells. "
        "We found that loop strength increased by 40% after cohesin depletion. "
        "These results demonstrate that loop extrusion drives compartment boundaries. "
        "Together, our findings reshape how genome folding is understood."
    )
    with db.session() as conn:
        db.upsert_papers(conn, [{
            "source": "pubmed", "ext_id": "1", "title": "Loop extrusion in single cells",
            "abstract": long_abstract, "summary": "A STALE SUMMARY.",
            "is_preprint": 0, "published_on": date.today().isoformat(),
            "authors": [], "topic_scores": {},
            "relevance": 0.5, "impact": 0.5, "score": 0.5, "scored_with": "old",
        }])

    rescore_all(config)
    with db.session() as conn:
        kept = conn.execute("SELECT summary FROM papers").fetchone()["summary"]
    assert kept == "A STALE SUMMARY."

    rescore_all(config, resummarize=True)
    with db.session() as conn:
        rebuilt = conn.execute("SELECT summary FROM papers").fetchone()["summary"]
    assert rebuilt != "A STALE SUMMARY."
    assert len(rebuilt) <= 200
