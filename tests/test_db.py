"""Storage, ranking windows and de-duplication."""

from datetime import date, timedelta

import pytest

from loki import db
from loki.pipeline import dedupe_against_db, windows


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def make(ext_id, *, score=0.5, day="2026-11-17", source="pubmed", preprint=False, doi=None):
    return {
        "source": source,
        "ext_id": ext_id,
        "doi": doi,
        "title": f"Paper {ext_id}",
        "abstract": "Abstract text.",
        "journal": None if preprint else "Nature",
        "journal_abbrev": None if preprint else "Nature",
        "issn": None if preprint else "0028-0836",
        "authors": ["Doe J"],
        "url": f"https://example.org/{ext_id}",
        "is_preprint": preprint,
        "published_on": day,
        "pub_date": day,
        "relevance": 0.9,
        "impact": 0.5,
        "impact_factor": None if preprint else 50.5,
        "score": score,
        "top_topic": "Genome editing",
        "topic_scores": {"Genome editing": 0.5},
        "scored_with": "fp1",
    }


# ---------------------------------------------------------------- upsert


def test_insert_then_update_does_not_duplicate(conn):
    assert db.upsert_papers(conn, [make("1")]) == (1, 0)
    assert db.upsert_papers(conn, [make("1", score=0.8)]) == (0, 1)
    assert db.stats(conn)["total_papers"] == 1
    row = conn.execute("SELECT score FROM papers").fetchone()
    assert row["score"] == 0.8


def test_same_id_on_different_sources_are_separate_papers(conn):
    db.upsert_papers(conn, [make("1", source="pubmed"), make("1", source="biorxiv")])
    assert db.stats(conn)["total_papers"] == 2


def test_json_fields_round_trip(conn):
    db.upsert_papers(conn, [make("1")])
    paper = db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30))[0]
    assert paper["authors"] == ["Doe J"]
    assert paper["topic_scores"] == {"Genome editing": 0.5}
    assert paper["is_preprint"] is False


def test_update_keeps_the_earliest_discovery_date(conn):
    db.upsert_papers(conn, [make("1", day="2026-11-10")])
    db.upsert_papers(conn, [make("1", day="2026-11-17")])
    row = conn.execute("SELECT published_on FROM papers").fetchone()
    # A paper must not drift forward into later digests each time it is re-seen.
    assert row["published_on"] == "2026-11-10"


def test_update_does_not_erase_an_abstract_with_null(conn):
    db.upsert_papers(conn, [make("1")])
    record = make("1")
    record["abstract"] = None
    db.upsert_papers(conn, [record])
    row = conn.execute("SELECT abstract FROM papers").fetchone()
    assert row["abstract"] == "Abstract text."


# ---------------------------------------------------------------- ranking


def test_top_papers_orders_by_score_and_respects_the_limit(conn):
    db.upsert_papers(conn, [make(str(i), score=i / 10) for i in range(1, 9)])
    top = db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30), limit=3)
    assert [p["score"] for p in top] == [0.8, 0.7, 0.6]


def test_window_boundaries_are_inclusive(conn):
    db.upsert_papers(
        conn,
        [
            make("before", day="2026-11-09"),
            make("start", day="2026-11-10"),
            make("end", day="2026-11-17"),
            make("after", day="2026-11-18"),
        ],
    )
    found = {
        p["ext_id"]
        for p in db.top_papers(conn, date(2026, 11, 10), date(2026, 11, 17), limit=50)
    }
    assert found == {"start", "end"}


def test_relevance_floor_filters(conn):
    low = make("low")
    low["relevance"] = 0.05
    db.upsert_papers(conn, [make("high"), low])
    top = db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30), min_relevance=0.15)
    assert [p["ext_id"] for p in top] == ["high"]


def test_unscored_papers_are_never_ranked(conn):
    unscored = make("x")
    unscored["score"] = None
    db.upsert_papers(conn, [unscored])
    assert db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30)) == []


# ---------------------------------------------------------------- windows


def test_window_spans():
    reference = date(2026, 11, 17)
    spans = windows(reference)
    assert spans["day"] == (reference, reference)
    assert spans["week"] == (reference - timedelta(days=6), reference)
    assert spans["month"] == (reference - timedelta(days=29), reference)


# ---------------------------------------------------------------- dedupe


def test_published_paper_replaces_its_preprint(conn):
    shared = "10.1038/x"
    db.upsert_papers(conn, [make("pre", source="biorxiv", preprint=True, doi=shared)])
    assert db.stats(conn)["total_papers"] == 1

    kept = dedupe_against_db(conn, [make("pub", doi=shared)])
    assert len(kept) == 1
    # The stored preprint is gone, so the digest shows one entry, not two.
    assert db.stats(conn)["total_papers"] == 0


def test_incoming_preprint_is_dropped_when_already_published(conn):
    shared = "10.1038/y"
    db.upsert_papers(conn, [make("pub", doi=shared)])
    kept = dedupe_against_db(conn, [make("pre", source="biorxiv", preprint=True, doi=shared)])
    assert kept == []


def test_dedupe_leaves_unrelated_records_alone(conn):
    kept = dedupe_against_db(conn, [make("a", doi="10.1/a"), make("b", doi="10.1/b")])
    assert len(kept) == 2


# ---------------------------------------------------------------- runs/meta


def test_meta_round_trip(conn):
    assert db.get_meta(conn, "missing", "fallback") == "fallback"
    db.set_meta(conn, "k", "v1")
    db.set_meta(conn, "k", "v2")
    assert db.get_meta(conn, "k") == "v2"


def test_run_lifecycle(conn):
    run_id = db.start_run(conn, date(2026, 11, 16), date(2026, 11, 17))
    db.finish_run(conn, run_id, "ok", fetched=10, inserted=7, updated=3)
    last = db.stats(conn)["last_run"]
    assert last["status"] == "ok"
    assert last["inserted"] == 7


def test_apply_scores_updates_rows(conn):
    db.upsert_papers(conn, [make("1")])
    paper_id = conn.execute("SELECT id FROM papers").fetchone()["id"]
    db.apply_scores(
        conn,
        [{"id": paper_id, "relevance": 0.1, "impact": 0.2, "impact_factor": None,
          "score": 0.13, "top_topic": "Other", "topic_scores": {}, "scored_with": "fp2"}],
    )
    row = conn.execute("SELECT score, scored_with FROM papers").fetchone()
    assert row["score"] == 0.13
    assert row["scored_with"] == "fp2"


def test_author_count_survives_a_round_trip(conn):
    db.upsert_papers(conn, [{
        "source": "pubmed", "ext_id": "crowd", "title": "A large collaboration",
        "authors": ["First A", "Second B", "Senior Z"], "author_count": 412,
        "is_preprint": False, "published_on": "2026-11-20", "pub_date": "2026-11-20",
        "relevance": 0.5, "impact": 0.5, "score": 0.5, "scored_with": "x",
        "tags": [], "topic_scores": {},
    }])
    paper = db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30), limit=1)[0]
    assert paper["author_count"] == 412
    assert paper["authors"][-1] == "Senior Z"


def test_a_paper_stored_before_the_column_existed_reads_back_as_none(conn):
    db.upsert_papers(conn, [{
        "source": "pubmed", "ext_id": "legacy", "title": "Older row",
        "authors": ["Only A"], "is_preprint": False,
        "published_on": "2026-11-20", "pub_date": "2026-11-20",
        "relevance": 0.5, "impact": 0.5, "score": 0.5, "scored_with": "x",
        "tags": [], "topic_scores": {},
    }])
    paper = db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30), limit=1)[0]
    assert paper["author_count"] is None
