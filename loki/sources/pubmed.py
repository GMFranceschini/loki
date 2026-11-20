"""Fetch peer-reviewed papers from PubMed via NCBI E-utilities.

Two steps per topic:
  esearch  find PMIDs entered into PubMed in the date window
  efetch   download full records in batches and parse the XML

Papers are searched by Entrez date (when PubMed indexed them), so the digest
reflects what is newly visible, not what carries an old cover date.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import httpx

from ..config import PubMedConfig, Topic
from .http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EFETCH_BATCH = 150
SOURCE = "pubmed"

# Record types that carry a journal's prestige but no science of their own.
# Without this filter a retraction notice in a high-impact journal outranks
# real papers, because the impact half of the score does not care about length.
EXCLUDED_PUB_TYPES = frozenset(
    t.lower()
    for t in (
        "Retraction of Publication",
        "Retracted Publication",
        "Published Erratum",
        "Comment",
        "Editorial",
        "Letter",
        "News",
        "Newspaper Article",
        "Biography",
        "Obituary",
        "Expression of Concern",
        "Corrected and Republished Article",
    )
)

# Titles of administrative notices, which are not always typed correctly.
EXCLUDED_TITLE_PREFIXES = (
    "retraction",
    "retracted",
    "erratum",
    "correction to",
    "corrigendum",
    "withdrawn",
    "expression of concern",
    "author correction",
    "publisher correction",
    "editorial expression of concern",
)


def _text(node: ET.Element | None) -> str:
    """All text inside a node, including nested markup like <i> or <sup>."""
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _first(node: ET.Element, *paths: str) -> ET.Element | None:
    for path in paths:
        found = node.find(path)
        if found is not None:
            return found
    return None


def _parse_date(node: ET.Element | None) -> str | None:
    """Turn a PubMed date node into YYYY-MM-DD, tolerating missing parts."""
    if node is None:
        return None
    year = _text(node.find("Year"))
    if not year.isdigit():
        # Some records only carry a free-text MedlineDate like "2026 Nov-Dec".
        medline = _text(node.find("MedlineDate"))
        year = medline[:4] if medline[:4].isdigit() else ""
        if not year:
            return None
        return f"{year}-01-01"

    raw_month = _text(node.find("Month")) or "1"
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    month = int(raw_month) if raw_month.isdigit() else months.get(raw_month[:3].lower(), 1)
    raw_day = _text(node.find("Day")) or "1"
    day = int(raw_day) if raw_day.isdigit() else 1

    try:
        return date(int(year), max(1, min(12, month)), max(1, min(31, day))).isoformat()
    except ValueError:
        return f"{year}-{max(1, min(12, month)):02d}-01"


def _abstract(article: ET.Element) -> str:
    """Join a structured abstract into one block, keeping section labels."""
    parts: list[str] = []
    for chunk in article.findall("Abstract/AbstractText"):
        body = _text(chunk)
        if not body:
            continue
        label = (chunk.get("Label") or "").strip()
        parts.append(f"{label.title()}: {body}" if label and label.upper() != "UNLABELLED" else body)
    return " ".join(parts)


def _authors(article: ET.Element, limit: int = 25) -> list[str]:
    names: list[str] = []
    for author in article.findall("AuthorList/Author"):
        if len(names) >= limit:
            break
        collective = _text(author.find("CollectiveName"))
        if collective:
            names.append(collective)
            continue
        last = _text(author.find("LastName"))
        initials = _text(author.find("Initials"))
        if last:
            names.append(f"{last} {initials}".strip())
    return names


def _doi(pubmed_article: ET.Element) -> str | None:
    for path in (
        "PubmedData/ArticleIdList/ArticleId[@IdType='doi']",
        "MedlineCitation/Article/ELocationID[@EIdType='doi']",
    ):
        value = _text(pubmed_article.find(path))
        if value:
            return value.lower()
    return None


def _entrez_date(pubmed_article: ET.Element) -> str | None:
    node = _first(
        pubmed_article,
        "PubmedData/History/PubMedPubDate[@PubStatus='entrez']",
        "PubmedData/History/PubMedPubDate[@PubStatus='pubmed']",
    )
    return _parse_date(node)


def _is_excluded(article: ET.Element, title: str) -> bool:
    """True for retractions, errata, editorials and similar notices."""
    for node in article.findall("PublicationTypeList/PublicationType"):
        if _text(node).lower() in EXCLUDED_PUB_TYPES:
            return True
    lowered = title.lower().lstrip("[ ")
    return lowered.startswith(EXCLUDED_TITLE_PREFIXES)


def parse_article(pubmed_article: ET.Element) -> dict[str, Any] | None:
    """Turn one <PubmedArticle> into a Loki record, or None to drop it."""
    citation = pubmed_article.find("MedlineCitation")
    if citation is None:
        return None
    article = citation.find("Article")
    if article is None:
        return None

    pmid = _text(citation.find("PMID"))
    title = _text(article.find("ArticleTitle")).rstrip(".")
    if not pmid or not title:
        return None
    if _is_excluded(article, title):
        return None

    journal = article.find("Journal")
    pub_date = _parse_date(
        _first(article, "ArticleDate")
    ) or _parse_date(_first(article, "Journal/JournalIssue/PubDate"))
    discovery = _entrez_date(pubmed_article) or pub_date

    if not discovery:
        return None

    return {
        "source": SOURCE,
        "ext_id": pmid,
        "doi": _doi(pubmed_article),
        "title": title,
        "abstract": _abstract(article) or None,
        "journal": _text(journal.find("Title")) if journal is not None else None,
        "journal_abbrev": _text(journal.find("ISOAbbreviation")) if journal is not None else None,
        "issn": _text(journal.find("ISSN")) if journal is not None else None,
        "authors": _authors(article),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "is_preprint": False,
        "published_on": discovery,
        "pub_date": pub_date,
    }


def _search(
    client: httpx.Client,
    query: str,
    start: date,
    end: date,
    retmax: int,
    limiter: RateLimiter,
    api_key: str,
) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(max(1, min(retmax, 10000))),
        "datetype": "edat",
        "mindate": start.strftime("%Y/%m/%d"),
        "maxdate": end.strftime("%Y/%m/%d"),
        "sort": "date",
    }
    if api_key:
        params["api_key"] = api_key

    resp = get_with_retry(client, f"{EUTILS}/esearch.fcgi", params, limiter)
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError:
        log.error("PubMed esearch returned non-JSON content")
        return []
    result = payload.get("esearchresult") or {}
    if "ERROR" in result:
        log.error("PubMed rejected the query: %s", result["ERROR"])
        return []
    return [pid for pid in result.get("idlist", []) if pid.isdigit()]


def _fetch_details(
    client: httpx.Client, pmids: list[str], limiter: RateLimiter, api_key: str
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for i in range(0, len(pmids), EFETCH_BATCH):
        batch = pmids[i : i + EFETCH_BATCH]
        params = {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}
        if api_key:
            params["api_key"] = api_key

        resp = get_with_retry(client, f"{EUTILS}/efetch.fcgi", params, limiter)
        if resp is None:
            log.error("Skipping a batch of %d PubMed records", len(batch))
            continue
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            log.error("PubMed efetch XML did not parse: %s", exc)
            continue

        for article in root.findall(".//PubmedArticle"):
            try:
                if record := parse_article(article):
                    records.append(record)
            except Exception as exc:
                log.warning("Skipping an unparseable PubMed record: %s", exc)
    return records


def fetch_pubmed(
    client: httpx.Client,
    topics: Iterable[Topic],
    start: date,
    end: date,
    cfg: PubMedConfig,
) -> list[dict[str, Any]]:
    """Fetch and de-duplicate PubMed records for every topic in the window."""
    api_key = cfg.resolved_api_key()
    # NCBI allows 3 requests/second without a key, 10 with one.
    limiter = RateLimiter(9.0 if api_key else 2.5)

    pmids: list[str] = []
    seen_ids: set[str] = set()
    for topic in topics:
        if not topic.pubmed:
            log.info("Topic %r has no PubMed query; skipping PubMed for it.", topic.name)
            continue
        found = _search(client, topic.pubmed, start, end, cfg.max_per_topic, limiter, api_key)
        fresh = [p for p in found if p not in seen_ids]
        seen_ids.update(fresh)
        pmids.extend(fresh)
        log.info("PubMed %-34s %4d hits (%d new)", topic.name, len(found), len(fresh))

    if not pmids:
        return []

    records = _fetch_details(client, pmids, limiter, api_key)
    log.info("PubMed: parsed %d of %d records", len(records), len(pmids))
    return records
