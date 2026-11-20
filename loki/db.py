"""SQLite storage for papers, scores and run bookkeeping."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator

from .paths import DB_FILE, ensure_dirs

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- pubmed | biorxiv | medrxiv
    ext_id        TEXT NOT NULL,          -- PMID or preprint DOI
    doi           TEXT,
    title         TEXT NOT NULL,
    abstract      TEXT,
    summary       TEXT,                   -- legacy: the old extracted summary.
                                          -- No longer written or served; kept so
                                          -- existing databases need no migration.
    journal       TEXT,                   -- NULL for preprints
    journal_abbrev TEXT,
    issn          TEXT,
    authors       TEXT,                   -- JSON array
    url           TEXT,
    is_preprint   INTEGER NOT NULL DEFAULT 0,
    published_on  TEXT NOT NULL,          -- YYYY-MM-DD, DISCOVERY date: drives
                                          -- the day/week/month digest windows
    pub_date      TEXT,                   -- YYYY-MM-DD, true publication date
    first_seen_at TEXT NOT NULL,          -- ISO-8601 UTC
    relevance     REAL,                   -- 0-1
    impact        REAL,                   -- 0-1
    impact_factor REAL,                   -- raw IF, NULL if unknown
    score         REAL,                   -- 0-1 combined
    top_topic     TEXT,
    tags          TEXT,                   -- JSON array of topic names
    topic_scores  TEXT,                   -- JSON {topic: sim}
    scored_with   TEXT,                   -- config fingerprint
    saved_at      TEXT,                   -- ISO-8601 UTC when starred, else
                                          -- NULL. A saved paper is kept out
                                          -- of the digest windows' reach and
                                          -- is never pruned.
    UNIQUE (source, ext_id)
);

CREATE INDEX IF NOT EXISTS idx_papers_rank ON papers (published_on, score DESC);
CREATE INDEX IF NOT EXISTS idx_papers_score ON papers (score DESC);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers (doi);
CREATE INDEX IF NOT EXISTS idx_papers_scored_with ON papers (scored_with);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    window_from TEXT,
    window_to   TEXT,
    fetched     INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Papers whose combined score is this good are surfaced even outside the top 10.
PAPER_COLUMNS = (
    "id, source, ext_id, doi, title, abstract, journal, journal_abbrev, issn, "
    "authors, url, is_preprint, published_on, pub_date, first_seen_at, "
    "relevance, impact, impact_factor, score, top_topic, tags, topic_scores, "
    "saved_at"
)

# Columns added after the first release. A database created by an earlier
# version has the table already, so CREATE TABLE IF NOT EXISTS skips it and
# the new column never appears. Each is added on open instead.
ADDED_COLUMNS = (("saved_at", "TEXT"),)

# Indexes on those columns, which therefore cannot be part of SCHEMA: it runs
# before the columns are added, and indexing one that is not there yet fails.
POST_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_papers_saved ON papers (saved_at);
"""

# Full-text search over title and abstract. This is an external-content table:
# it stores only the index and reads the text from `papers`, so the abstracts
# are not duplicated on disk. Triggers keep it in step.
#
# The tokenizer does not stem. Stemming and the prefix search a search box
# needs pull against each other: porter indexes "methylation" under the stem
# "methyl", so a prefix query for "methylat" matches nothing. Raw tokens plus
# a prefix term cover the same ground more predictably, because "methylat"
# then finds methylation, methylated and methylate alike.
#
# The update trigger names its columns (AFTER UPDATE OF title, abstract).
# Without that it would fire on every score write, and a rescore touches every
# row, so each rescore would needlessly rebuild the whole index.
# Raise this whenever FTS_SCHEMA changes in a way that makes the stored index
# wrong, for instance a different tokenizer. The next open rebuilds.
FTS_VERSION = "1-unicode61-prefix345"
META_FTS_VERSION = "fts_version"

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5 (
    title, abstract, content='papers', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2',
    prefix='3 4 5'
);

CREATE TRIGGER IF NOT EXISTS papers_fts_insert AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts (rowid, title, abstract)
    VALUES (new.id, new.title, new.abstract);
END;

CREATE TRIGGER IF NOT EXISTS papers_fts_delete AFTER DELETE ON papers BEGIN
    INSERT INTO papers_fts (papers_fts, rowid, title, abstract)
    VALUES ('delete', old.id, old.title, old.abstract);
END;

CREATE TRIGGER IF NOT EXISTS papers_fts_update
AFTER UPDATE OF title, abstract ON papers BEGIN
    INSERT INTO papers_fts (papers_fts, rowid, title, abstract)
    VALUES ('delete', old.id, old.title, old.abstract);
    INSERT INTO papers_fts (rowid, title, abstract)
    VALUES (new.id, new.title, new.abstract);
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(path or DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.executescript(POST_SCHEMA)
    _init_fts(conn)
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current column list."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(papers)")}
    for name, decl in ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
            log.info("Added the %s column to papers", name)


def has_fts(conn: sqlite3.Connection) -> bool:
    """Whether the full-text index exists and can be queried."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'papers_fts'"
    ).fetchone()
    return bool(row)


def _init_fts(conn: sqlite3.Connection) -> None:
    """Create the search index, and build it when it is new or out of date.

    FTS5 is compiled into every SQLite that Python ships on macOS and Linux,
    but it is an optional module. If it is missing, search falls back to a
    LIKE scan rather than the whole app failing to open the database.

    The build is driven by a recorded version rather than by inspecting the
    index. On an external-content table SELECT COUNT(*) reads the content
    table, so it reports every stored paper whether the index holds anything
    or not, and cannot tell a filled index from an empty one. The version also
    gives any later change to the tokenizer a way to force the rebuild it
    needs: raise FTS_VERSION and the next open re-indexes.
    """
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        log.warning("No full-text index (%s); search will scan instead", exc)
        return

    if get_meta(conn, META_FTS_VERSION) == FTS_VERSION:
        return
    stored = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    if stored:
        # 'rebuild' discards the index and reads the content table back in.
        conn.execute("INSERT INTO papers_fts (papers_fts) VALUES ('rebuild')")
        log.info("Built the full-text index over %d stored papers", stored)
    set_meta(conn, META_FTS_VERSION, FTS_VERSION)


@contextmanager
def session(path=None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- meta


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- papers


def upsert_papers(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Insert new papers, refresh metadata and scores on ones already stored.

    Returns (inserted, updated).
    """
    inserted = updated = 0
    sql_insert = """
        INSERT INTO papers (
            source, ext_id, doi, title, abstract, journal, journal_abbrev, issn,
            authors, url, is_preprint, published_on, pub_date, first_seen_at,
            relevance, impact, impact_factor, score, top_topic, tags,
            topic_scores, scored_with
        ) VALUES (
            :source, :ext_id, :doi, :title, :abstract, :journal, :journal_abbrev,
            :issn, :authors, :url, :is_preprint, :published_on, :pub_date,
            :first_seen_at, :relevance, :impact, :impact_factor, :score,
            :top_topic, :tags, :topic_scores, :scored_with
        )
        ON CONFLICT (source, ext_id) DO UPDATE SET
            doi           = COALESCE(excluded.doi, papers.doi),
            title         = excluded.title,
            abstract      = COALESCE(excluded.abstract, papers.abstract),
            journal       = COALESCE(excluded.journal, papers.journal),
            journal_abbrev = COALESCE(excluded.journal_abbrev, papers.journal_abbrev),
            issn          = COALESCE(excluded.issn, papers.issn),
            authors       = excluded.authors,
            url           = COALESCE(excluded.url, papers.url),
            published_on  = MIN(papers.published_on, excluded.published_on),
            pub_date      = COALESCE(excluded.pub_date, papers.pub_date),
            relevance     = excluded.relevance,
            impact        = excluded.impact,
            impact_factor = excluded.impact_factor,
            score         = excluded.score,
            top_topic     = excluded.top_topic,
            tags          = excluded.tags,
            topic_scores  = excluded.topic_scores,
            scored_with   = excluded.scored_with
    """
    now = utcnow()
    for rec in records:
        row = dict(rec)
        row.setdefault("first_seen_at", now)
        for optional in ("doi", "abstract", "journal", "journal_abbrev", "issn",
                         "url", "pub_date", "impact_factor", "top_topic"):
            row.setdefault(optional, None)
        if isinstance(row.get("authors"), (list, tuple)):
            row["authors"] = json.dumps(list(row["authors"]))
        if isinstance(row.get("topic_scores"), dict):
            row["topic_scores"] = json.dumps(row["topic_scores"])
        if isinstance(row.get("tags"), (list, tuple)):
            row["tags"] = json.dumps(list(row["tags"]))
        row.setdefault("tags", "[]")
        row["is_preprint"] = int(bool(row.get("is_preprint")))

        exists = conn.execute(
            "SELECT 1 FROM papers WHERE source = ? AND ext_id = ?",
            (row["source"], row["ext_id"]),
        ).fetchone()
        conn.execute(sql_insert, row)
        if exists:
            updated += 1
        else:
            inserted += 1
    return inserted, updated


def _row_to_paper(row: sqlite3.Row) -> dict[str, Any]:
    paper = dict(row)
    for key in ("authors", "tags", "topic_scores"):
        empty = {} if key == "topic_scores" else []
        raw = paper.get(key)
        if isinstance(raw, str) and raw:
            try:
                paper[key] = json.loads(raw)
            except json.JSONDecodeError:
                paper[key] = empty
        elif raw is None:
            paper[key] = empty
    paper["is_preprint"] = bool(paper.get("is_preprint"))
    return paper


def top_papers(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    limit: int = 10,
    min_relevance: float = 0.0,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Highest scoring papers published in [start, end] inclusive."""
    sql = (
        f"SELECT {PAPER_COLUMNS} FROM papers "
        "WHERE published_on >= ? AND published_on <= ? "
        "AND score IS NOT NULL AND relevance >= ?"
    )
    params: list[Any] = [start.isoformat(), end.isoformat(), min_relevance]
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        params.extend(sources)
    sql += " ORDER BY score DESC, published_on DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_paper(r) for r in conn.execute(sql, params)]


def count_in_window(conn: sqlite3.Connection, start: date, end: date, min_relevance: float = 0.0) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE published_on >= ? AND published_on <= ? "
        "AND score IS NOT NULL AND relevance >= ?",
        (start.isoformat(), end.isoformat(), min_relevance),
    ).fetchone()
    return int(row["n"])


def iter_all_papers(conn: sqlite3.Connection, batch: int = 500) -> Iterator[list[sqlite3.Row]]:
    """Stream every stored paper, for rescoring."""
    cur = conn.execute(
        "SELECT id, title, abstract, journal, journal_abbrev, issn, is_preprint "
        "FROM papers"
    )
    while rows := cur.fetchmany(batch):
        yield rows


def apply_scores(conn: sqlite3.Connection, scored: Iterable[dict[str, Any]]) -> int:
    rows = [
        (
            s["relevance"],
            s["impact"],
            s.get("impact_factor"),
            s["score"],
            s.get("top_topic"),
            json.dumps(s.get("tags") or []),
            json.dumps(s.get("topic_scores") or {}),
            s["scored_with"],
            s["id"],
        )
        for s in scored
    ]
    conn.executemany(
        "UPDATE papers SET relevance = ?, impact = ?, impact_factor = ?, score = ?, "
        "top_topic = ?, tags = ?, topic_scores = ?, "
        "scored_with = ? WHERE id = ?",
        rows,
    )
    return len(rows)


def delete_before(conn: sqlite3.Connection, cutoff: date) -> int:
    """Remove papers published before `cutoff`. Returns the number deleted.

    A saved paper is never deleted. Saving one is a statement that it matters
    beyond the window it was found in, which is exactly the thing retention
    would otherwise take away.
    """
    cur = conn.execute(
        "DELETE FROM papers WHERE published_on < ? AND saved_at IS NULL",
        (cutoff.isoformat(),),
    )
    return cur.rowcount or 0


def count_before(conn: sqlite3.Connection, cutoff: date) -> int:
    """Papers a prune at `cutoff` would delete. Excludes saved ones."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE published_on < ? AND saved_at IS NULL",
        (cutoff.isoformat(),),
    ).fetchone()
    return int(row["n"])


def count_saved_before(conn: sqlite3.Connection, cutoff: date) -> int:
    """Saved papers older than `cutoff`, which pruning keeps regardless."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE published_on < ? AND saved_at IS NOT NULL",
        (cutoff.isoformat(),),
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- saved


def set_saved(conn: sqlite3.Connection, paper_id: int, saved: bool) -> dict[str, Any] | None:
    """Star or unstar one paper. Returns the stored paper, or None if absent.

    Re-saving an already saved paper keeps the original date, so the Saved
    list does not reshuffle when a button is pressed twice.
    """
    if saved:
        conn.execute(
            "UPDATE papers SET saved_at = ? WHERE id = ? AND saved_at IS NULL",
            (utcnow(), paper_id),
        )
    else:
        conn.execute("UPDATE papers SET saved_at = NULL WHERE id = ?", (paper_id,))
    row = conn.execute(
        f"SELECT {PAPER_COLUMNS} FROM papers WHERE id = ?", (paper_id,)
    ).fetchone()
    return _row_to_paper(row) if row else None


def saved_papers(
    conn: sqlite3.Connection, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """Saved papers, most recently saved first.

    No relevance floor and no date window: a saved paper is shown because it
    was saved, not because it still scores well or falls in a digest.
    """
    return [
        _row_to_paper(r)
        for r in conn.execute(
            f"SELECT {PAPER_COLUMNS} FROM papers WHERE saved_at IS NOT NULL "
            "ORDER BY saved_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    ]


def count_saved(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE saved_at IS NOT NULL"
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- search

# FTS5 MATCH takes a query language of its own, where a bare hyphen, quote or
# asterisk from a search box is either a syntax error or a silent change of
# meaning. Rather than pass the text through, it is rebuilt from its parts.
_PHRASE = re.compile(r'"([^"]*)"')
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)

SEARCH_ORDERS = ("score", "match", "date")


def fts_query(raw: str) -> str:
    """Turn a search box string into a safe FTS5 MATCH expression.

    Quoted runs stay phrases. Every other word becomes a prefix term, so
    "methylat" finds methylation, and the terms are ANDed: more words mean
    fewer, better results, which is how a search box is expected to behave.
    Returns "" when nothing searchable is left.
    """
    parts: list[str] = []

    def phrase(text: str) -> None:
        words = _WORD.findall(text)
        if words:
            parts.append('"' + " ".join(words) + '"')

    for chunk in _PHRASE.findall(raw or ""):
        phrase(chunk)
    for word in _WORD.findall(_PHRASE.sub(" ", raw or "")):
        parts.append('"' + word + '"*')
    return " AND ".join(parts)


def search_papers(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 25,
    offset: int = 0,
    order: str = "score",
    sources: list[str] | None = None,
    saved_only: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Papers whose title or abstract match `query`. Returns (papers, total).

    Searching deliberately ignores the relevance floor and the digest windows.
    The ranked lists answer "what should I read now"; search answers "what do
    I have", and hiding stored papers from it would make it a liar.
    """
    if order not in SEARCH_ORDERS:
        raise ValueError(f"Unknown order {order!r}; use one of {', '.join(SEARCH_ORDERS)}")

    where = ["1 = 1"]
    params: list[Any] = []
    using_fts = has_fts(conn)
    expression = fts_query(query) if using_fts else ""

    if using_fts and expression:
        source = (
            "papers JOIN papers_fts ON papers_fts.rowid = papers.id "
            "AND papers_fts MATCH ?"
        )
        params.append(expression)
    else:
        # No index, or nothing left after cleaning: scan instead. Slower, but
        # it keeps search working on a SQLite built without FTS5.
        source = "papers"
        terms = _WORD.findall(query or "")
        if not terms:
            return [], 0
        for term in terms[:8]:
            where.append("(papers.title LIKE ? OR papers.abstract LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%"])

    if saved_only:
        where.append("papers.saved_at IS NOT NULL")
    if sources:
        where.append(f"(papers.source IN ({','.join('?' * len(sources))}))")
        params.extend(sources)

    clause = " AND ".join(where)
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS n FROM {source} WHERE {clause}", params
        ).fetchone()["n"]
    )

    ordering = {
        "score": "papers.score IS NULL, papers.score DESC, papers.published_on DESC",
        "date": "papers.published_on DESC, papers.score DESC",
        "match": (
            "bm25(papers_fts, 3.0, 1.0)" if using_fts and expression
            else "papers.score IS NULL, papers.score DESC"
        ),
    }[order]

    columns = ", ".join(f"papers.{c.strip()}" for c in PAPER_COLUMNS.split(","))
    rows = conn.execute(
        f"SELECT {columns} FROM {source} WHERE {clause} "
        f"ORDER BY {ordering}, papers.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [_row_to_paper(r) for r in rows], total


def trim_runs(conn: sqlite3.Connection, keep: int) -> int:
    """Keep only the newest `keep` rows in the run log."""
    if keep <= 0:
        return 0
    cur = conn.execute(
        "DELETE FROM runs WHERE id NOT IN "
        "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    return cur.rowcount or 0


def clear_legacy_summaries(conn: sqlite3.Connection) -> int:
    """Blank the unused summary column.

    Nothing writes or serves it since the card began showing the abstract
    itself, but databases created earlier still carry the text.
    """
    cur = conn.execute("UPDATE papers SET summary = NULL WHERE summary IS NOT NULL")
    return cur.rowcount or 0


def vacuum(path=None) -> None:
    """Return freed pages to the filesystem.

    VACUUM cannot run inside a transaction, so this takes its own connection
    in autocommit mode rather than joining an open session.
    """
    conn = connect(path)
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
    finally:
        conn.close()


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    by_source = {
        r["source"]: r["n"]
        for r in conn.execute("SELECT source, COUNT(*) AS n FROM papers GROUP BY source")
    }
    span = conn.execute(
        "SELECT MIN(published_on) AS lo, MAX(published_on) AS hi FROM papers"
    ).fetchone()
    last_run = conn.execute(
        "SELECT started_at, finished_at, status, fetched, inserted, updated, detail "
        "FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    saved = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE saved_at IS NOT NULL"
    ).fetchone()["n"]
    return {
        "total_papers": int(total),
        "saved_papers": int(saved),
        "by_source": by_source,
        "earliest": span["lo"],
        "latest": span["hi"],
        "last_run": dict(last_run) if last_run else None,
    }


# ---------------------------------------------------------------- runs


def start_run(conn: sqlite3.Connection, window_from: date, window_to: date) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, window_from, window_to) VALUES (?, ?, ?)",
        (utcnow(), window_from.isoformat(), window_to.isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    detail: str = "",
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, fetched = ?, inserted = ?, "
        "updated = ?, detail = ? WHERE id = ?",
        (utcnow(), status, fetched, inserted, updated, detail[:4000], run_id),
    )
    conn.commit()


def recent_runs(conn: sqlite3.Connection, limit: int = 15) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    ]
