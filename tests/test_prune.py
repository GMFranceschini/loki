"""Retention: keeping the database from growing without limit."""

from datetime import date, timedelta

import pytest

from loki import db
from loki.config import Config, EmbeddingConfig, Scoring, StorageConfig, Topic
from loki.pipeline import minimum_retention_days, prune, today_utc, trim_logs, windows

from test_db import make


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway database that pipeline.prune will act on."""
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "test.db")
    return tmp_path


def config(retention=60, padding=10, keep_runs=200, vacuum=False):
    return Config(
        topics=(Topic("Hi-C", 1.0, "Chromosome conformation capture.", ""),),
        scoring=Scoring(window_padding_days=padding),
        embedding=EmbeddingConfig(backend="hashing"),
        storage=StorageConfig(
            retention_days=retention, keep_runs=keep_runs, vacuum=vacuum
        ),
    )


def seed(days_ago: list[int]):
    with db.session() as conn:
        today = today_utc()
        db.upsert_papers(
            conn,
            [
                make(str(n), day=(today - timedelta(days=n)).isoformat())
                for n in days_ago
            ],
        )


# ---------------------------------------------------------------- the floor


def test_minimum_retention_matches_the_deepest_window():
    cfg = config(padding=10)
    assert minimum_retention_days(cfg) == 39          # month: 30 - 1 + 10

    reference = date(2026, 11, 20)
    month_start = windows(reference, cfg.scoring.padding_by_window)["month"][0]
    assert (reference - month_start).days == minimum_retention_days(cfg)


def test_retention_is_never_shorter_than_the_windows_reach(store):
    """Asking for 5 days must not delete papers the month list still shows."""
    seed([0, 20, 35, 50])
    result = prune(config(retention=5), dry_run=True)
    assert result["retention_days"] == 39
    assert result["clamped_to_window_reach"] is True
    assert result["papers_would_delete"] == 1          # only the 50-day-old one


def test_a_paper_the_month_list_shows_survives_pruning(store):
    cfg = config(retention=5)
    seed([0, 38])
    prune(cfg)
    reference = today_utc()
    start, end = windows(reference, cfg.scoring.padding_by_window)["month"]
    with db.session() as conn:
        assert db.count_in_window(conn, start, end) == 2


# ---------------------------------------------------------------- pruning


def test_old_papers_go_and_recent_ones_stay(store):
    seed([1, 30, 80, 400])
    result = prune(config(retention=60))
    assert result["papers_deleted"] == 2
    with db.session() as conn:
        kept = [r["ext_id"] for r in conn.execute("SELECT ext_id FROM papers ORDER BY ext_id")]
    assert kept == ["1", "30"]


def test_a_dry_run_deletes_nothing(store):
    seed([1, 400])
    result = prune(config(retention=60), dry_run=True)
    assert result["papers_would_delete"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM papers").fetchone()["n"] == 2


def test_zero_retention_keeps_everything(store):
    seed([1, 4000])
    result = prune(config(retention=0))
    assert result["status"] == "disabled"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM papers").fetchone()["n"] == 2


def test_older_than_overrides_the_config(store):
    seed([1, 45, 400])
    result = prune(config(retention=365), older_than=40)
    assert result["papers_deleted"] == 2       # 45 and 400 days old


# ---------------------------------------------------------------- run log


def test_the_run_log_is_trimmed_to_the_newest_rows(store):
    with db.session() as conn:
        for _ in range(12):
            db.start_run(conn, date(2026, 11, 1), date(2026, 11, 2))
    prune(config(keep_runs=5))
    with db.session() as conn:
        rows = db.recent_runs(conn, 50)
    assert len(rows) == 5
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)


# ---------------------------------------------------------------- legacy column


def test_the_unused_summary_column_is_blanked(store):
    seed([1])
    with db.session() as conn:
        conn.execute("UPDATE papers SET summary = 'an orphaned summary'")
    result = prune(config())
    assert result["legacy_summaries_cleared"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT summary FROM papers").fetchone()["summary"] is None


# ---------------------------------------------------------------- logs


def test_an_oversized_log_is_trimmed_to_its_tail(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("loki.paths.LOG_DIR", log_dir)
    big = log_dir / "app.log"
    big.write_bytes(b"".join(f"line {i}\n".encode() for i in range(50_000)))
    before = big.stat().st_size

    cfg = Config(
        topics=(Topic("Hi-C", 1.0, "Chromosome conformation capture.", ""),),
        storage=StorageConfig(max_log_bytes=10_000),
    )
    assert trim_logs(cfg) == 1
    assert big.stat().st_size < before
    # The newest lines survive; the oldest are gone.
    text = big.read_text()
    assert "line 49999" in text
    assert "line 0\n" not in text


def test_a_small_log_is_left_alone(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("loki.paths.LOG_DIR", log_dir)
    small = log_dir / "app.log"
    small.write_text("one line\n")
    cfg = Config(
        topics=(Topic("Hi-C", 1.0, "Chromosome conformation capture.", ""),),
        storage=StorageConfig(max_log_bytes=10_000),
    )
    assert trim_logs(cfg) == 0
    assert small.read_text() == "one line\n"


# ---------------------------------------------------------------- saved papers


def test_pruning_keeps_a_saved_paper(store):
    """Saving a paper is how it outlives the window it was found in."""
    seed([400, 500])
    with db.session() as conn:
        kept = conn.execute("SELECT id FROM papers ORDER BY id").fetchone()["id"]
        db.set_saved(conn, kept, True)

    result = prune(config(retention=60))
    assert result["papers_deleted"] == 1
    assert result["saved_kept"] == 1
    with db.session() as conn:
        assert db.count_saved(conn) == 1
        assert db.stats(conn)["total_papers"] == 1


def test_a_dry_run_says_what_it_would_spare(store):
    seed([400])
    with db.session() as conn:
        db.set_saved(conn, conn.execute("SELECT id FROM papers").fetchone()["id"], True)

    result = prune(config(retention=60), dry_run=True)
    assert result["papers_would_delete"] == 0
    assert result["saved_kept"] == 1
