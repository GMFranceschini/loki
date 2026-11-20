"""Full-text search, and the saved-paper flag."""

from datetime import date, timedelta

import pytest

from loki import db

from test_db import make


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "test.db")
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def paper(ext_id, title, abstract, **kw):
    record = make(ext_id, **kw)
    record["title"] = title
    record["abstract"] = abstract
    return record


def seed(conn):
    db.upsert_papers(
        conn,
        [
            paper("1", "Loop extrusion by cohesin",
                  "Cohesin extrudes loops of chromatin.", score=0.3),
            paper("2", "DNA methylation clocks",
                  "Methylation of CpG islands predicts age.", score=0.9),
            paper("3", "Methylated cytosine mapping",
                  "A method for 5-methylcytosine.", score=0.4, day="2026-11-10"),
        ],
    )


# ---------------------------------------------------------------- the query


def test_a_word_fragment_matches_what_it_starts(conn):
    seed(conn)
    papers, total = db.search_papers(conn, "methylat")
    assert total == 2
    assert {p["ext_id"] for p in papers} == {"2", "3"}


def test_several_words_narrow_rather_than_widen(conn):
    seed(conn)
    assert db.search_papers(conn, "cohesin chromatin")[1] == 1
    # "chromatin" alone is in one paper, "methylation" in another: together,
    # ANDed, they are in none.
    assert db.search_papers(conn, "chromatin methylation")[1] == 0


def test_a_quoted_phrase_stays_a_phrase(conn):
    seed(conn)
    assert db.search_papers(conn, '"loop extrusion"')[1] == 1
    assert db.search_papers(conn, '"extrusion loop"')[1] == 0


def test_the_abstract_is_searched_too(conn):
    seed(conn)
    assert db.search_papers(conn, "CpG")[1] == 1


def test_fts_syntax_from_the_search_box_is_not_executed(conn):
    seed(conn)
    # Each of these is a syntax error or an operator in the FTS5 query
    # language. A search box must survive them, not raise.
    for raw in ['"', "AND", "*", "-cohesin", 'NEAR("a" "b")', "a OR b", "^x", "{}"]:
        papers, total = db.search_papers(conn, raw)
        assert total >= 0 and isinstance(papers, list)


def test_an_unsearchable_query_finds_nothing(conn):
    seed(conn)
    assert db.search_papers(conn, "   ")[1] == 0
    assert db.search_papers(conn, "")[1] == 0


def test_fts_query_builds_prefix_terms_and_phrases():
    assert db.fts_query("loop extrusion") == '"loop"* AND "extrusion"*'
    assert db.fts_query('"loop extrusion" cohesin') == '"loop extrusion" AND "cohesin"*'
    assert db.fts_query("") == ""
    assert db.fts_query("-- *") == ""


# ---------------------------------------------------------------- ordering


def test_order_by_score_puts_the_best_paper_first(conn):
    seed(conn)
    papers, _ = db.search_papers(conn, "methylat", order="score")
    assert [p["ext_id"] for p in papers] == ["2", "3"]


def test_order_by_date_puts_the_newest_first(conn):
    seed(conn)
    papers, _ = db.search_papers(conn, "methylat", order="date")
    assert [p["ext_id"] for p in papers] == ["2", "3"]


def test_order_by_match_is_accepted(conn):
    seed(conn)
    papers, _ = db.search_papers(conn, "methylat", order="match")
    assert {p["ext_id"] for p in papers} == {"2", "3"}


def test_an_unknown_order_is_refused(conn):
    with pytest.raises(ValueError, match="Unknown order"):
        db.search_papers(conn, "x", order="sideways")


def test_limit_and_offset_page_through_the_matches(conn):
    seed(conn)
    first, total = db.search_papers(conn, "methylat", limit=1)
    second, _ = db.search_papers(conn, "methylat", limit=1, offset=1)
    assert total == 2
    assert first[0]["ext_id"] != second[0]["ext_id"]


def test_search_ignores_the_relevance_floor(conn):
    """Search answers "what do I have", so a low score must not hide a paper."""
    low = paper("low", "Cohesin review", "A review of cohesin.", score=0.01)
    low["relevance"] = 0.0
    db.upsert_papers(conn, [low])
    assert db.search_papers(conn, "cohesin")[1] == 1
    # The same paper is invisible to the ranked lists.
    assert db.top_papers(conn, date(2026, 11, 1), date(2026, 11, 30), min_relevance=0.15) == []


def test_sources_can_be_restricted(conn):
    db.upsert_papers(conn, [
        paper("j", "Cohesin in journals", "Text.", source="pubmed"),
        paper("p", "Cohesin in preprints", "Text.", source="biorxiv", preprint=True),
    ])
    assert db.search_papers(conn, "cohesin", sources=["biorxiv", "medrxiv"])[1] == 1
    assert db.search_papers(conn, "cohesin", sources=["pubmed"])[1] == 1


# ---------------------------------------------------------------- the index


def test_the_index_follows_a_title_change(conn):
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '1'").fetchone()["id"]
    conn.execute("UPDATE papers SET title = 'Condensin loops' WHERE id = ?", (paper_id,))
    assert db.search_papers(conn, "condensin")[1] == 1
    assert db.search_papers(conn, "extrusion")[1] == 0


def test_the_index_follows_a_deletion(conn):
    seed(conn)
    conn.execute("DELETE FROM papers WHERE ext_id = '2'")
    assert db.search_papers(conn, "clocks")[1] == 0


def test_a_score_update_leaves_the_index_alone(conn):
    """A rescore writes every row. Re-indexing all of them each time is waste."""
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '1'").fetchone()["id"]
    db.apply_scores(conn, [{
        "id": paper_id, "relevance": 0.5, "impact": 0.5, "impact_factor": None,
        "score": 0.5, "top_topic": None, "tags": [], "topic_scores": {},
        "scored_with": "fp2",
    }])
    assert db.search_papers(conn, "extrusion")[1] == 1


def test_a_database_from_before_the_index_gets_one(tmp_path, monkeypatch):
    """An existing database has the papers table already, so the migration,
    not CREATE TABLE, is what has to add the column and build the index."""
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_FILE", path)
    old = db.connect(path)
    # The schema as it stood before saving and searching existed.
    old.executescript(
        "CREATE TABLE papers (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
        "ext_id TEXT NOT NULL, doi TEXT, title TEXT NOT NULL, abstract TEXT, "
        "summary TEXT, journal TEXT, journal_abbrev TEXT, issn TEXT, authors TEXT, "
        "url TEXT, is_preprint INTEGER NOT NULL DEFAULT 0, published_on TEXT NOT NULL, "
        "pub_date TEXT, first_seen_at TEXT NOT NULL, relevance REAL, impact REAL, "
        "impact_factor REAL, score REAL, top_topic TEXT, tags TEXT, "
        "topic_scores TEXT, scored_with TEXT, UNIQUE (source, ext_id));"
        "INSERT INTO papers (source, ext_id, title, abstract, published_on, "
        "first_seen_at, score, relevance) VALUES "
        "('pubmed', '9', 'Loop extrusion', 'Cohesin.', '2026-11-17', '2026-11-17', 0.5, 0.9);"
    )
    old.commit()
    old.close()

    with db.session() as conn:
        assert "saved_at" in {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
        assert db.has_fts(conn)
        assert db.search_papers(conn, "extrusion")[1] == 1
        assert db.count_saved(conn) == 0


def test_search_still_works_without_an_index(conn, monkeypatch):
    """FTS5 is an optional SQLite module. Without it, search scans."""
    seed(conn)
    monkeypatch.setattr(db, "has_fts", lambda _conn: False)
    papers, total = db.search_papers(conn, "methylation")
    assert total == 1
    assert papers[0]["ext_id"] == "2"
    # The scan is a substring match, so a fragment still finds its word.
    assert db.search_papers(conn, "methylat")[1] == 2


# ---------------------------------------------------------------- saving


def test_saving_and_unsaving_one_paper(conn):
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '1'").fetchone()["id"]

    saved = db.set_saved(conn, paper_id, True)
    assert saved["saved_at"]
    assert db.count_saved(conn) == 1
    assert db.stats(conn)["saved_papers"] == 1

    unsaved = db.set_saved(conn, paper_id, False)
    assert unsaved["saved_at"] is None
    assert db.count_saved(conn) == 0


def test_saving_twice_keeps_the_first_date(conn):
    """Otherwise the Saved list reshuffles when a button is pressed twice."""
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '1'").fetchone()["id"]
    first = db.set_saved(conn, paper_id, True)["saved_at"]
    assert db.set_saved(conn, paper_id, True)["saved_at"] == first


def test_saving_a_paper_that_is_not_there(conn):
    assert db.set_saved(conn, 4242, True) is None


def test_the_saved_list_is_newest_first(conn):
    seed(conn)
    ids = [r["id"] for r in conn.execute("SELECT id FROM papers ORDER BY ext_id")]
    for paper_id, when in zip(ids, ("2026-11-01T00:00:00+00:00",
                                    "2026-11-03T00:00:00+00:00",
                                    "2026-11-02T00:00:00+00:00")):
        conn.execute("UPDATE papers SET saved_at = ? WHERE id = ?", (when, paper_id))
    assert [p["ext_id"] for p in db.saved_papers(conn)] == ["2", "3", "1"]


def test_the_saved_list_ignores_score_and_date(conn):
    """A saved paper is listed because it was saved, not because it ranks."""
    old = paper("old", "Ancient cohesin paper", "Text.", day="2019-01-01", score=0.0)
    old["relevance"] = 0.0
    db.upsert_papers(conn, [old])
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = 'old'").fetchone()["id"]
    db.set_saved(conn, paper_id, True)
    assert [p["ext_id"] for p in db.saved_papers(conn)] == ["old"]


def test_search_can_be_held_to_saved_papers(conn):
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '2'").fetchone()["id"]
    db.set_saved(conn, paper_id, True)
    assert db.search_papers(conn, "methylat", saved_only=True)[1] == 1
    assert db.search_papers(conn, "cohesin", saved_only=True)[1] == 0


def test_a_refetch_does_not_unsave_a_paper(conn):
    """The same paper comes back on the next run and is updated in place."""
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '1'").fetchone()["id"]
    db.set_saved(conn, paper_id, True)
    db.upsert_papers(conn, [paper("1", "Loop extrusion by cohesin", "Revised text.")])
    assert db.count_saved(conn) == 1


def test_saved_papers_survive_pruning(conn):
    seed(conn)
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = '1'").fetchone()["id"]
    db.set_saved(conn, paper_id, True)
    conn.execute("UPDATE papers SET published_on = '2019-01-01'")
    cutoff = date(2026, 1, 1)

    assert db.count_before(conn, cutoff) == 2
    assert db.count_saved_before(conn, cutoff) == 1
    assert db.delete_before(conn, cutoff) == 2
    assert [p["ext_id"] for p in db.saved_papers(conn)] == ["1"]


def test_paper_rows_carry_the_saved_flag(conn):
    seed(conn)
    today = date.today()
    db.upsert_papers(conn, [paper("new", "Cohesin today", "Text.", day=today.isoformat())])
    paper_id = conn.execute("SELECT id FROM papers WHERE ext_id = 'new'").fetchone()["id"]
    db.set_saved(conn, paper_id, True)
    rows = db.top_papers(conn, today - timedelta(days=1), today)
    assert rows and rows[0]["saved_at"]
