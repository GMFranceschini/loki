"""FastAPI app: JSON API plus the static web UI."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .config import Config, ConfigError, load_config, save_topics
from .paths import INTERESTS_FILE, WEB_DIR
from .pipeline import digest, fetch_and_rescore, rescore_all, today_utc

log = logging.getLogger(__name__)

WINDOWS = ("day", "week", "month")

# Loki listens on the loopback interface, which is not by itself a boundary a
# browser respects. Two attacks reach a localhost server from an ordinary web
# page the user happens to have open:
#
#   DNS rebinding   A hostname the attacker controls resolves to 127.0.0.1,
#                   so their page becomes same-origin with Loki and can read
#                   every response. The Host header still carries their name.
#   Cross-site POST A plain form or fetch POST needs no preflight, so any page
#                   can trigger one. It cannot read the reply, but /api/fetch
#                   and /api/topics change state, which is enough.
#
# Rejecting requests that do not address a loopback name closes the first;
# rejecting an unsafe method carrying a foreign Origin closes the second.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", ""})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(value: str | None) -> str:
    """The host part of a Host header or an Origin URL, lowercased."""
    if not value:
        return ""
    if "://" in value:
        from urllib.parse import urlparse

        return (urlparse(value).hostname or "").lower()
    host = value.strip().lower()
    if host.startswith("["):  # [::1]:8713
        return host[1 : host.index("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if ":" in host else host


def _allowed_hosts() -> frozenset[str]:
    """Loopback names, plus anything LOKI_ALLOWED_HOSTS adds.

    Set it when reaching Loki through another name on purpose, for example a
    reverse proxy or a container: LOKI_ALLOWED_HOSTS=loki.lan,10.0.0.4
    """
    extra = os.environ.get("LOKI_ALLOWED_HOSTS", "")
    return LOOPBACK_NAMES | {h.strip().lower() for h in extra.split(",") if h.strip()}



app = FastAPI(title="Loki", version=__version__, docs_url="/api/docs", redoc_url=None)

# One fetch at a time. A second request gets told one is already running.
_fetch_lock = asyncio.Lock()
_fetch_log: list[str] = []


@app.middleware("http")
async def only_from_this_machine(request, call_next):
    allowed = _allowed_hosts()

    host = _hostname(request.headers.get("host"))
    if host not in allowed:
        log.warning("Refused a request addressed to host %r", host)
        return JSONResponse(
            status_code=421,
            content={
                "detail": (
                    f"Loki only answers to a loopback address, not {host!r}. "
                    "Set LOKI_ALLOWED_HOSTS if you meant to reach it by that name."
                )
            },
        )

    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and _hostname(origin) not in allowed:
            log.warning("Refused a %s from origin %r", request.method, origin)
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site requests are not accepted."},
            )

    return await call_next(request)


def _config() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Config error: {exc}") from exc


def _parse_date(raw: str | None) -> date:
    if not raw:
        return today_utc()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Date {raw!r} is not in YYYY-MM-DD form"
        ) from None


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.get("/api/config")
def read_config() -> dict[str, Any]:
    cfg = _config()
    return {
        "topics": [
            {"name": t.name, "weight": t.weight, "prose": t.prose} for t in cfg.topics
        ],
        "scoring": {
            "relevance_weight": cfg.scoring.relevance_weight,
            "impact_weight": cfg.scoring.impact_weight,
            "min_relevance": cfg.scoring.min_relevance,
            "window_padding_days": cfg.scoring.padding_by_window,
            "preprint_impact": cfg.scoring.preprint_impact,
            "impact_factor_cap": cfg.scoring.impact_factor_cap,
        },
        "sources": {
            "pubmed": cfg.pubmed.enabled,
            "preprints": list(cfg.biorxiv.servers) if cfg.biorxiv.enabled else [],
        },
        "embedding": {"backend": cfg.embedding.backend, "model": cfg.embedding.model},
    }


@app.get("/api/topics")
def read_topics() -> dict[str, Any]:
    """The topic profile, as the editor in the web UI shows it."""
    cfg = _config()
    return {
        "topics": [
            {
                "name": t.name,
                "weight": t.weight,
                "prose": t.prose,
                "pubmed": t.pubmed,
            }
            for t in cfg.topics
        ],
        "path": str(INTERESTS_FILE),
    }


@app.put("/api/topics")
async def write_topics(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Save the topic profile, then rescore every stored paper.

    Changing topics changes what relevance means, so stored scores would
    otherwise be measured against a profile that no longer exists.
    """
    if _fetch_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A fetch is running. Wait for it to finish, then save again.",
        )

    try:
        saved = save_topics(payload.get("topics"))
    except ConfigError as exc:
        # The message is written for the person editing, so pass it through.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not write {INTERESTS_FILE}: {exc}"
        ) from exc

    async with _fetch_lock:
        try:
            config = load_config()
            rescored = await asyncio.to_thread(rescore_all, config)
        except Exception as exc:
            log.exception("Rescore after a topic change failed")
            return {
                "status": "saved_not_rescored",
                "topics": saved,
                "detail": f"Topics were saved, but rescoring failed: {exc}",
            }

    return {
        "status": "ok",
        "topics": saved,
        "rescored": rescored,
        "detail": (
            f"Saved {len(saved)} topics and rescored {rescored} stored papers. "
            "Run a fetch to pull in papers matching any new topic."
        ),
    }


@app.get("/api/digest")
def read_digest(
    window: str = Query("day", pattern="^(day|week|month)$"),
    date_: str | None = Query(None, alias="date"),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    cfg = _config()
    reference = _parse_date(date_)
    with db.session() as conn:
        result = digest(
            conn, window, reference, limit, cfg.scoring.min_relevance,
            cfg.scoring.padding_by_window,
        )
        result["stats"] = db.stats(conn)
    return result


@app.get("/api/digests")
def read_all_digests(
    date_: str | None = Query(None, alias="date"),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """All three windows in one call, so the UI loads in a single request."""
    cfg = _config()
    reference = _parse_date(date_)
    with db.session() as conn:
        payload = {
            "reference": reference.isoformat(),
            "limit": limit,
            "digests": {
                w: digest(
                    conn, w, reference, limit, cfg.scoring.min_relevance,
                    cfg.scoring.padding_by_window,
                )
                for w in WINDOWS
            },
            "stats": db.stats(conn),
            "topics": [t.name for t in cfg.topics],
            "weights": {
                "relevance": cfg.scoring.relevance_weight,
                "impact": cfg.scoring.impact_weight,
            },
            "padding_days": cfg.scoring.padding_by_window,
        }
    return payload


@app.get("/api/stats")
def read_stats() -> dict[str, Any]:
    with db.session() as conn:
        payload = db.stats(conn)
        payload["runs"] = db.recent_runs(conn, 10)
        payload["embedder"] = db.get_meta(conn, "embedder")
        payload["last_fetch_at"] = db.get_meta(conn, "last_fetch_at")
    return payload


@app.get("/api/coverage")
def read_coverage(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Papers per day, so the UI can show gaps in coverage."""
    end = today_utc()
    start = end - timedelta(days=days - 1)
    cfg = _config()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT published_on AS day, COUNT(*) AS total, "
            "SUM(CASE WHEN relevance >= ? THEN 1 ELSE 0 END) AS relevant "
            "FROM papers WHERE published_on >= ? AND published_on <= ? "
            "GROUP BY published_on ORDER BY published_on",
            (cfg.scoring.min_relevance, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": [dict(r) for r in rows],
    }


@app.post("/api/fetch")
async def trigger_fetch(backfill: int = Query(2, ge=0, le=90)) -> JSONResponse:
    """Run a fetch now. Returns 409 if one is already in flight."""
    if _fetch_lock.locked():
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "detail": "A fetch is already running", "log": _fetch_log[-20:]},
        )

    async with _fetch_lock:
        _fetch_log.clear()
        cfg = _config()

        def progress(message: str) -> None:
            _fetch_log.append(message)

        try:
            summary = await asyncio.to_thread(
                fetch_and_rescore, config=cfg, backfill_days=backfill, progress=progress
            )
        except Exception as exc:
            log.exception("Fetch triggered from the UI failed")
            return JSONResponse(
                status_code=500,
                content={"status": "error", "detail": str(exc), "log": _fetch_log[-20:]},
            )
        summary["log"] = list(_fetch_log)
        return JSONResponse(content=summary)


@app.get("/api/fetch/log")
def fetch_log() -> dict[str, Any]:
    return {"running": _fetch_lock.locked(), "log": _fetch_log[-50:]}


# ------------------------------------------------------------------ web UI

if WEB_DIR.exists():
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
else:  # pragma: no cover
    @app.get("/")
    def index_missing() -> dict[str, str]:
        return {"detail": f"No web directory at {WEB_DIR}"}
