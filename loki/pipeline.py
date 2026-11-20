"""The fetch-score-store pipeline, and rescoring of stored papers."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import db
from .config import Config, load_config
from .scoring import Scorer
from .scoring import background as bg
from .sources import build_client, fetch_biorxiv, fetch_pubmed

log = logging.getLogger(__name__)

# PubMed keeps indexing papers for days after they appear, so each run re-reads
# a few days back. Re-seen papers are updated, not duplicated.
DEFAULT_LOOKBACK_DAYS = 2


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def build_scorer(config: Config, conn=None) -> Scorer:
    """Build a Scorer with the stored background vector applied."""
    embedder = None
    vector = None
    if conn is not None:
        from .scoring.embed import get_embedder

        embedder = get_embedder(config.embedding.backend, config.embedding.model)
        vector, _ = bg.load(conn, embedder.dim, embedder.name)
    return Scorer(config, embedder=embedder, background=vector)


def refresh_background(conn, scorer: Scorer) -> bool:
    """Re-estimate the background vector if the corpus has grown enough.

    Returns True when it changed, which means stored scores are stale.
    """
    corpus = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    current = int(db.get_meta(conn, bg.META_COUNT, "0") or 0)
    owned_by = db.get_meta(conn, bg.META_EMBEDDER)
    has_vector = scorer.background is not None and owned_by == scorer.embedder.name

    if not bg.should_refresh(current, corpus, has_vector):
        return False

    vector, count = bg.compute(conn, scorer.embedder)
    if vector is None:
        return False
    bg.save(conn, vector, count, scorer.embedder.name)
    conn.commit()
    log.info("Background vector refreshed from %d papers", count)
    return True


def dedupe_against_db(conn, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a preprint when the same DOI is already stored as a journal paper.

    Preprints get published. Without this the digest shows the preprint and the
    final paper as two separate entries.
    """
    dois = {r["doi"] for r in records if r.get("doi") and not r.get("is_preprint")}
    if dois:
        placeholders = ",".join("?" * len(dois))
        conn.execute(
            f"DELETE FROM papers WHERE is_preprint = 1 AND doi IN ({placeholders})",
            list(dois),
        )

    published_dois: set[str] = set()
    for row in conn.execute("SELECT doi FROM papers WHERE is_preprint = 0 AND doi IS NOT NULL"):
        published_dois.add(row["doi"])

    kept, dropped = [], 0
    for rec in records:
        if rec.get("is_preprint") and rec.get("doi") in published_dois:
            dropped += 1
            continue
        kept.append(rec)
    if dropped:
        log.info("Dropped %d preprints already stored as journal papers", dropped)
    return kept


def run_fetch(
    config: Config | None = None,
    start: date | None = None,
    end: date | None = None,
    backfill_days: int = 0,
    scorer: Scorer | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch, score and store papers for a date window.

    Returns a summary dict. Never raises for a single-source failure: a
    PubMed outage must not lose the preprints that did come back.
    """
    config = config or load_config()
    end = end or today_utc()
    start = start or (end - timedelta(days=max(backfill_days, DEFAULT_LOOKBACK_DAYS)))
    if start > end:
        start, end = end, start

    def say(message: str) -> None:
        log.info(message)
        if progress:
            progress(message)

    say(f"Fetching {start} to {end}")

    errors: list[str] = []
    records: list[dict[str, Any]] = []

    with db.session() as conn:
        scorer = scorer or build_scorer(config, conn)
        run_id = db.start_run(conn, start, end)
        try:
            with build_client(config.pubmed.contact_email) as client:
                if config.pubmed.enabled:
                    try:
                        fetched = fetch_pubmed(client, config.topics, start, end, config.pubmed)
                        records.extend(fetched)
                        say(f"PubMed: {len(fetched)} records")
                    except Exception as exc:
                        log.exception("PubMed fetch failed")
                        errors.append(f"pubmed: {exc}")
                        say(f"PubMed failed: {exc}")

                if config.biorxiv.enabled:
                    try:
                        fetched = fetch_biorxiv(client, start, end, config.biorxiv)
                        records.extend(fetched)
                        say(f"Preprints: {len(fetched)} records")
                    except Exception as exc:
                        log.exception("Preprint fetch failed")
                        errors.append(f"biorxiv: {exc}")
                        say(f"Preprint fetch failed: {exc}")

            # Within-batch de-duplication by (source, ext_id).
            unique: dict[tuple[str, str], dict[str, Any]] = {}
            for rec in records:
                unique[(rec["source"], rec["ext_id"])] = rec
            records = list(unique.values())

            records = dedupe_against_db(conn, records)

            say(f"Scoring {len(records)} papers")
            scorer.score_records(records)

            # Strip the transient category key the preprint parser adds.
            for rec in records:
                rec.pop("category", None)

            inserted, updated = db.upsert_papers(conn, records)
            db.set_meta(conn, "last_fetch_at", db.utcnow())
            db.set_meta(conn, "config_fingerprint", scorer.fingerprint)
            db.set_meta(conn, "embedder", scorer.embedder.name)

            status = "ok" if not errors else "error"
            detail = "; ".join(errors)
            db.finish_run(conn, run_id, status, len(records), inserted, updated, detail)
            say(f"Stored: {inserted} new, {updated} updated")

            above = sum(1 for r in records if r["relevance"] >= config.scoring.min_relevance)
            background_changed = refresh_background(conn, scorer)
            return {
                "background_refreshed": background_changed,
                "status": status,
                "window": {"from": start.isoformat(), "to": end.isoformat()},
                "fetched": len(records),
                "inserted": inserted,
                "updated": updated,
                "above_threshold": above,
                "errors": errors,
            }
        except Exception as exc:
            log.exception("Fetch run failed")
            db.finish_run(conn, run_id, "error", len(records), detail=str(exc))
            raise


def fetch_and_rescore(
    config: Config | None = None,
    progress: Callable[[str], None] | None = None,
    **kwargs,
) -> dict[str, Any]:
    """A fetch, plus a full rescore when the background vector moved.

    The background is what makes relevance comparable between papers, so when
    it changes every stored score must be recomputed or the digest mixes two
    different scales.
    """
    config = config or load_config()
    summary = run_fetch(config=config, progress=progress, **kwargs)
    if summary.get("background_refreshed"):
        if progress:
            progress("Background vector changed; rescoring the whole corpus")
        summary["rescored"] = rescore_all(config)
    return summary


def rescore_all(
    config: Config | None = None,
    scorer: Scorer | None = None,
    resummarize: bool = False,
) -> int:
    """Recompute relevance, impact and score for every stored paper.

    Run this after editing interests.yaml or journals.json.

    A paper's summary is normally left alone: summarising re-embeds every
    sentence of every abstract, which is far slower than rescoring, and the
    summary does not depend on the topics or the journal table. Pass
    resummarize=True after changing the `summary:` settings, which is the one
    case where the stored summaries really are stale.
    """
    config = config or load_config()
    from .scoring.score import paper_text

    total = 0
    with db.session() as conn:
        scorer = scorer or build_scorer(config, conn)
        for rows in db.iter_all_papers(conn):
            texts = [paper_text(r["title"], r["abstract"]) for r in rows]
            relevance, topics, sims, tags = scorer.relevance(texts)
            payload = []
            for row, rel, topic, topic_sims, tag_list in zip(
                rows, relevance, topics, sims, tags
            ):
                imp, jif = scorer.impact(
                    row["journal"], row["issn"], bool(row["is_preprint"]), row["journal_abbrev"]
                )
                payload.append(
                    {
                        "id": row["id"],
                        "relevance": round(float(rel), 4),
                        "impact": round(float(imp), 4),
                        "impact_factor": jif,
                        "score": round(scorer.combine(float(rel), float(imp)), 4),
                        "top_topic": topic,
                        "tags": tag_list,
                        "summary": (
                            scorer.summarize(row["abstract"], row["title"])
                            if resummarize
                            else row["summary"]
                            or scorer.summarize(row["abstract"], row["title"])
                        ),
                        "topic_scores": topic_sims,
                        "scored_with": scorer.fingerprint,
                    }
                )
            total += db.apply_scores(conn, payload)
            log.info("Rescored %d papers", total)
        db.set_meta(conn, "config_fingerprint", scorer.fingerprint)
        db.set_meta(conn, "embedder", scorer.embedder.name)
    return total


BASE_WINDOW_DAYS = {"day": 1, "week": 7, "month": 30}


def windows(
    reference: date, padding_days: int | Mapping[str, int] = 0
) -> dict[str, tuple[date, date]]:
    """The three digest windows, each ending at the reference date.

    A window reaches `padding_days` further back than its nominal length. A
    paper worth reading today was often posted or indexed a few days earlier,
    and journals backdate, so a strict calendar day is too tight a net.
    `padding_days` is either one number for every window, or a mapping of
    window name to days, so the day list can be held tighter than the month.

    The padding only reaches backwards. Nothing is published after the
    reference date, so widening a window forwards asks for papers from the
    future: it can only ever return records whose date is wrong.

    The windows stay nested: whatever is in the day list is also in the week
    and month lists. Per-window padding could break that — a heavily padded
    day and an unpadded week would cross — so a wider window is pulled back
    to its neighbour's start rather than allowed to sit inside it.
    """

    def pad_for(name: str) -> int:
        value = padding_days.get(name, 0) if isinstance(padding_days, Mapping) else padding_days
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    spans: dict[str, tuple[date, date]] = {}
    previous_start: date | None = None
    # BASE_WINDOW_DAYS is ordered shortest first, which is what keeps the
    # nesting check below a single comparison against the window before.
    for name, length in BASE_WINDOW_DAYS.items():
        start = reference - timedelta(days=length - 1 + pad_for(name))
        if previous_start is not None and start > previous_start:
            start = previous_start
        previous_start = start
        spans[name] = (start, reference)
    return spans


def digest(
    conn,
    window: str = "day",
    reference: date | None = None,
    limit: int = 10,
    min_relevance: float = 0.0,
    padding_days: int | Mapping[str, int] = 0,
) -> dict[str, Any]:
    """Top papers for one window."""
    reference = reference or today_utc()
    spans = windows(reference, padding_days)
    if window not in spans:
        raise ValueError(f"Unknown window {window!r}; use one of {', '.join(spans)}")
    start, end = spans[window]
    papers = db.top_papers(conn, start, end, limit, min_relevance)
    return {
        "window": window,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "reference": reference.isoformat(),
        # Derived from the span rather than the setting, so it still reads
        # true when nesting pulled this window back further than asked.
        "padding_days": (end - start).days + 1 - BASE_WINDOW_DAYS[window],
        "base_days": BASE_WINDOW_DAYS[window],
        "considered": db.count_in_window(conn, start, end, min_relevance),
        "count": len(papers),
        "papers": papers,
    }
