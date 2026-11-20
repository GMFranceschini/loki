"""SQLite storage for papers, scores and run bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator

from .paths import DB_FILE, ensure_dirs

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
    "relevance, impact, impact_factor, score, top_topic, tags, topic_scores"
)


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
    conn.commit()


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
    return {
        "total_papers": int(total),
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
