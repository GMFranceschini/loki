"""Fetch preprints from bioRxiv and medRxiv.

Their API has no keyword search: you ask for a date range and get every
preprint posted in it, 30 at a time behind a cursor. That suits Loki, because
embedding relevance then filters the whole day's output rather than relying on
keyword queries.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Iterable

import httpx

from ..config import BioRxivConfig
from .http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

API = "https://api.biorxiv.org/details"
PAGE = 100  # The API caps a page at 100 and defaults to 30.
VALID_SERVERS = ("biorxiv", "medrxiv")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _iso_date(value: Any) -> str | None:
    """Accept only a real YYYY-MM-DD date.

    A length check is not enough: "not-a-date" is also ten characters, and a
    bad value here would silently land a paper in the wrong digest window.
    """
    text = _clean(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _split_authors(raw: Any, limit: int = 25) -> list[str]:
    """The API returns one semicolon-delimited string of authors."""
    parts = [p.strip() for p in _clean(raw).split(";") if p.strip()]
    return parts[:limit]


def parse_preprint(item: dict[str, Any], server: str) -> dict[str, Any] | None:
    doi = _clean(item.get("doi")).lower()
    title = _clean(item.get("title")).rstrip(".")
    posted = _iso_date(item.get("date"))
    if not doi or not title or not posted:
        return None

    version = _clean(item.get("version"))
    return {
        "source": server,
        "ext_id": doi,
        "doi": doi,
        "title": title,
        "abstract": _clean(item.get("abstract")) or None,
        # Preprints are unjournalled by definition. Keeping journal NULL is
        # what makes the scorer apply the flat preprint impact score.
        "journal": None,
        "journal_abbrev": None,
        "issn": None,
        "authors": _split_authors(item.get("authors")),
        "url": f"https://www.{server}.org/content/{doi}v{version or '1'}",
        "is_preprint": True,
        "published_on": posted,
        "pub_date": posted,
        "category": _clean(item.get("category")).lower() or None,
    }


def _fetch_server(
    client: httpx.Client,
    server: str,
    start: date,
    end: date,
    categories: set[str],
    max_records: int,
    limiter: RateLimiter,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = 0
    total: int | None = None

    while len(records) < max_records:
        url = f"{API}/{server}/{start.isoformat()}/{end.isoformat()}/{cursor}"
        resp = get_with_retry(client, url, {"format": "json"}, limiter)
        if resp is None:
            log.error("%s: giving up at cursor %d", server, cursor)
            break
        try:
            payload = resp.json()
        except ValueError:
            log.error("%s returned non-JSON content at cursor %d", server, cursor)
            break

        messages = payload.get("messages") or [{}]
        status = _clean(messages[0].get("status")).lower()
        if status and status != "ok":
            # "no posts found" is the normal end-of-range answer.
            if "no posts" not in status:
                log.warning("%s API said: %s", server, status)
            break

        if total is None:
            try:
                total = int(messages[0].get("total") or 0)
            except (TypeError, ValueError):
                total = 0

        collection = payload.get("collection") or []
        if not collection:
            break

        for item in collection:
            record = parse_preprint(item, server)
            if not record:
                continue
            # Later versions of the same preprint reuse the DOI; keep the newest.
            if record["ext_id"] in seen:
                continue
            if categories and (record.get("category") or "") not in categories:
                continue
            seen.add(record["ext_id"])
            records.append(record)

        cursor += len(collection)
        if total and cursor >= total:
            break
        if len(collection) < 30:  # short page means the range is exhausted
            break

    log.info(
        "%-8s %4d preprints kept%s",
        server,
        len(records),
        f" (of {total} posted)" if total else "",
    )
    return records[:max_records]


def fetch_biorxiv(
    client: httpx.Client,
    start: date,
    end: date,
    cfg: BioRxivConfig,
) -> list[dict[str, Any]]:
    """Fetch preprints posted in [start, end] from every configured server."""
    limiter = RateLimiter(2.0)
    categories = {c.strip().lower() for c in (cfg.categories or ()) if c and c.strip()}

    servers: Iterable[str] = [s.lower().strip() for s in (cfg.servers or VALID_SERVERS)]
    servers = [s for s in servers if s in VALID_SERVERS] or list(VALID_SERVERS)

    budget = max(1, cfg.max_per_run)
    per_server = max(1, budget // len(servers))

    records: list[dict[str, Any]] = []
    for server in servers:
        records.extend(
            _fetch_server(client, server, start, end, categories, per_server, limiter)
        )
    return records
