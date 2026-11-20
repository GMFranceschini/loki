"""HTTP API behaviour, driven against a temporary database and config."""

import pytest
from fastapi.testclient import TestClient

GOOD_PROSE = "CRISPR genome editing and base editing in primary human cells and tissues."


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A server wired to a throwaway database and config file."""
    config_path = tmp_path / "interests.yaml"
    config_path.write_text(
        "topics:\n"
        "  - name: Genome editing\n"
        "    weight: 1.0\n"
        f"    prose: {GOOD_PROSE}\n"
        "    pubmed: CRISPR[tiab]\n"
        "scoring:\n"
        "  min_relevance: 0.0\n"
        "embedding:\n"
        "  backend: hashing\n",
        encoding="utf-8",
    )

    from loki import db, server
    from loki.scoring import impact

    monkeypatch.setattr("loki.config.INTERESTS_FILE", config_path)
    monkeypatch.setattr(server, "INTERESTS_FILE", config_path)
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "test.db")
    monkeypatch.setattr(impact, "JOURNALS_FILE", tmp_path / "missing.json")

    with TestClient(server.app, base_url="http://127.0.0.1:8713") as test_client:
        yield test_client


def seed(client, count=3):
    from datetime import date

    from loki import db

    records = [
        {
            "source": "pubmed", "ext_id": str(i), "doi": None,
            "title": f"Paper {i}", "abstract": "Text.", "journal": "Nature",
            "journal_abbrev": "Nature", "issn": "0028-0836", "authors": ["Doe J"],
            "url": f"https://example.org/{i}", "is_preprint": False,
            "published_on": date.today().isoformat(), "pub_date": date.today().isoformat(),
            "relevance": 0.9, "impact": 0.5, "impact_factor": 50.5,
            "score": 0.5 + i / 100, "top_topic": "Genome editing",
            "topic_scores": {}, "scored_with": "fp",
        }
        for i in range(count)
    ]
    with db.session() as conn:
        db.upsert_papers(conn, records)


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_config_endpoint_reports_topics_and_weights(client):
    payload = client.get("/api/config").json()
    assert payload["topics"][0]["name"] == "Genome editing"
    assert payload["scoring"]["relevance_weight"] == 0.7


def test_digests_returns_all_three_windows(client):
    payload = client.get("/api/digests").json()
    assert set(payload["digests"]) == {"day", "week", "month"}
    assert payload["topics"] == ["Genome editing"]


def test_digest_ranks_and_limits(client):
    seed(client, 5)
    payload = client.get("/api/digest?window=week&limit=2").json()
    scores = [p["score"] for p in payload["papers"]]
    assert len(scores) == 2
    assert scores == sorted(scores, reverse=True)


def test_digest_rejects_an_unknown_window(client):
    assert client.get("/api/digest?window=fortnight").status_code == 422


def test_digest_rejects_a_bad_date(client):
    response = client.get("/api/digest?date=17-11-2026")
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_empty_database_returns_an_empty_digest(client):
    payload = client.get("/api/digest?window=day").json()
    assert payload["papers"] == []
    assert payload["considered"] == 0


def test_topics_can_be_read(client):
    payload = client.get("/api/topics").json()
    assert payload["topics"][0]["prose"].startswith("CRISPR")


def test_topics_can_be_saved_and_persist(client):
    response = client.put(
        "/api/topics",
        json={
            "topics": [
                {"name": "RNA biology", "weight": 2.0,
                 "prose": "RNA modification, splicing regulation and RNA-protein interactions."},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # The change is visible on the next read, so it really was written to disk.
    assert client.get("/api/topics").json()["topics"][0]["name"] == "RNA biology"


def test_saving_topics_rescores_stored_papers(client):
    seed(client, 2)
    response = client.put(
        "/api/topics",
        json={"topics": [{"name": "New topic", "weight": 1.0,
                          "prose": "Completely different subject matter about soil chemistry."}]},
    )
    assert response.json()["rescored"] == 2


def test_invalid_topics_are_rejected_with_a_readable_message(client):
    response = client.put("/api/topics", json={"topics": [{"name": "X", "prose": "tiny"}]})
    assert response.status_code == 400
    assert "longer description" in response.json()["detail"]


def test_rejected_topics_do_not_change_the_saved_profile(client):
    client.put("/api/topics", json={"topics": [{"name": "A", "prose": "short"}]})
    assert client.get("/api/topics").json()["topics"][0]["name"] == "Genome editing"


def test_stats_endpoint(client):
    seed(client, 2)
    payload = client.get("/api/stats").json()
    assert payload["total_papers"] == 2
    assert payload["by_source"] == {"pubmed": 2}


def test_coverage_endpoint(client):
    seed(client, 2)
    payload = client.get("/api/coverage?days=7").json()
    assert payload["days"][-1]["total"] == 2


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Loki" in response.text


# --------------------------------------------------------------- local-only guard
#
# Loki binds to loopback, which a browser does not treat as a boundary on its
# own. These cover the two ways an ordinary web page can reach it anyway.


def test_a_foreign_host_header_is_refused(client):
    """DNS rebinding: the attacker's name resolves to 127.0.0.1."""
    response = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 421
    assert "loopback" in response.json()["detail"]


def test_loopback_host_headers_are_accepted(client):
    for host in ("127.0.0.1:8713", "localhost:8713", "[::1]:8713"):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_a_cross_site_post_is_refused(client):
    """A page on another origin can POST without a preflight; it must not."""
    response = client.post("/api/fetch", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]


def test_the_page_itself_may_post(client, monkeypatch):
    """The UI sends its own Origin on a POST, and must still be served."""
    monkeypatch.setattr(
        "loki.server.fetch_and_rescore",
        lambda **kwargs: {"status": "ok", "fetched": 0, "inserted": 0, "updated": 0},
    )
    response = client.post("/api/fetch", headers={"Origin": "http://127.0.0.1:8713"})
    assert response.status_code == 200


def test_a_cross_site_get_is_still_served(client):
    """Only unsafe methods are origin-checked; a GET is harmless on its own."""
    response = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 200


def test_allowed_hosts_can_be_extended(client, monkeypatch):
    monkeypatch.setenv("LOKI_ALLOWED_HOSTS", "loki.lan")
    assert client.get("/api/health", headers={"Host": "loki.lan"}).status_code == 200


# ---------------------------------------------------------------- search


def test_search_finds_a_stored_paper(client):
    seed(client)
    payload = client.get("/api/search", params={"q": "Paper"}).json()
    assert payload["total"] == 3
    assert payload["indexed"] is True
    assert len(payload["papers"]) == 3


def test_search_needs_something_to_look_for(client):
    assert client.get("/api/search", params={"q": "   "}).status_code == 400


def test_search_rejects_an_unknown_order(client):
    assert client.get("/api/search", params={"q": "x", "order": "sideways"}).status_code == 422


def test_search_can_be_limited_to_one_source(client):
    seed(client)
    payload = client.get("/api/search", params={"q": "Paper", "source": "preprint"}).json()
    assert payload["total"] == 0


# ---------------------------------------------------------------- saving


def _first_paper_id(client):
    return client.get("/api/digest", params={"window": "day"}).json()["papers"][0]["id"]


def test_saving_a_paper_then_reading_it_back(client):
    seed(client)
    paper_id = _first_paper_id(client)

    response = client.post(f"/api/papers/{paper_id}/save", json={"saved": True})
    assert response.status_code == 200
    assert response.json()["paper"]["saved_at"]

    listing = client.get("/api/saved").json()
    assert listing["total"] == 1
    assert listing["papers"][0]["id"] == paper_id

    assert client.post(f"/api/papers/{paper_id}/save", json={"saved": False}).status_code == 200
    assert client.get("/api/saved").json()["total"] == 0


def test_saving_defaults_to_saving(client):
    seed(client)
    paper_id = _first_paper_id(client)
    assert client.post(f"/api/papers/{paper_id}/save", json={}).json()["paper"]["saved_at"]


def test_saving_a_paper_that_is_not_there(client):
    assert client.post("/api/papers/4242/save", json={"saved": True}).status_code == 404


def test_the_saved_count_reaches_the_stats(client):
    seed(client)
    client.post(f"/api/papers/{_first_paper_id(client)}/save", json={"saved": True})
    assert client.get("/api/stats").json()["saved_papers"] == 1


def test_an_empty_saved_list_is_not_an_error(client):
    payload = client.get("/api/saved").json()
    assert payload == {"count": 0, "total": 0, "limit": 100, "offset": 0, "papers": []}


# ---------------------------------------------------------------- weights


def test_the_topic_editor_reports_the_current_balance(client):
    weights = client.get("/api/topics").json()["weights"]
    assert weights == {"relevance_weight": 0.7, "impact_weight": 0.3}


def test_saving_a_new_balance_rewrites_the_config(client):
    topics = client.get("/api/topics").json()["topics"]
    response = client.put(
        "/api/topics",
        json={"topics": topics, "weights": {"relevance_weight": 0.4, "impact_weight": 0.6}},
    )
    assert response.status_code == 200
    assert client.get("/api/topics").json()["weights"] == {
        "relevance_weight": 0.4, "impact_weight": 0.6
    }


def test_a_balance_of_nothing_is_refused(client):
    topics = client.get("/api/topics").json()["topics"]
    response = client.put(
        "/api/topics",
        json={"topics": topics, "weights": {"relevance_weight": 0, "impact_weight": 0}},
    )
    assert response.status_code == 400
    assert "weight" in response.json()["detail"]
    # The refused save must leave the file as it was.
    assert client.get("/api/topics").json()["weights"]["relevance_weight"] == 0.7


def test_omitting_the_balance_leaves_it_alone(client):
    topics = client.get("/api/topics").json()["topics"]
    assert client.put("/api/topics", json={"topics": topics}).status_code == 200
    assert client.get("/api/topics").json()["weights"]["relevance_weight"] == 0.7


# ---------------------------------------------------------------- caching


def test_the_ui_files_are_revalidated_not_guessed(client):
    """A stale UI looks broken rather than cached.

    With no Cache-Control a browser picks a freshness lifetime from how long
    the file had gone unchanged, so an edit to a long-stable app.js does not
    appear on reload. no-cache keeps the copy but checks it every time.
    """
    for path in ("/", "/static/app.js", "/static/styles.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path
