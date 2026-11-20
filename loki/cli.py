"""Command line entry point: loki fetch | serve | top | rescore | stats | doctor."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta

from . import __version__, db
from .config import ConfigError, load_config
from .paths import DB_FILE, INTERESTS_FILE, LOG_DIR, PID_FILE, ensure_dirs


def _setup_logging(verbose: bool, to_file: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        ensure_dirs()
        handlers.append(logging.FileHandler(LOG_DIR / "loki.log"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # httpx logs every request at INFO, which drowns out the useful lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _parse_day(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a YYYY-MM-DD date") from None


# ---------------------------------------------------------------- commands


def cmd_fetch(args) -> int:
    from .pipeline import fetch_and_rescore

    config = load_config()
    summary = fetch_and_rescore(
        config=config,
        start=args.start,
        end=args.end,
        backfill_days=args.backfill,
        progress=None,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("status") == "ok" else 1


def cmd_rescore(args) -> int:
    from .pipeline import rescore_all

    total = rescore_all(load_config())
    print(f"Rescored {total} papers")
    return 0


def cmd_prune(args) -> int:
    from .pipeline import prune, trim_logs

    config = load_config()
    result = prune(config, older_than=args.older_than, dry_run=args.dry_run)

    if result["status"] == "disabled":
        print(result["detail"])
        return 0

    if result.get("clamped_to_window_reach"):
        print(
            f"Keeping {result['retention_days']} days: that is as short as the "
            "digest windows allow, so nothing they could still show is deleted."
        )

    if args.dry_run:
        print(
            f"Would delete {result['papers_would_delete']} papers published "
            f"before {result['cutoff']}."
        )
        if result.get("saved_kept"):
            print(f"{result['saved_kept']} saved paper(s) that old would be kept.")
        return 0

    print(
        f"Deleted {result['papers_deleted']} papers published before "
        f"{result['cutoff']}, and {result['runs_deleted']} old run records."
    )
    if result.get("saved_kept"):
        print(f"Kept {result['saved_kept']} saved paper(s) older than the cutoff.")
    if result["legacy_summaries_cleared"]:
        print(f"Cleared {result['legacy_summaries_cleared']} unused summaries.")
    if result["vacuumed"]:
        print("Reclaimed the freed space.")
    trimmed = trim_logs(config)
    if trimmed:
        print(f"Trimmed {trimmed} oversized log file(s).")
    return 0


def _print_papers(papers, show_saved: bool = False) -> None:
    """One block per paper, as `top` has always printed them."""
    for i, paper in enumerate(papers, 1):
        venue = paper["journal"] or f"{paper['source']} (preprint)"
        jif = f"IF {paper['impact_factor']:.1f}" if paper["impact_factor"] else "no IF"
        star = " ★" if show_saved and paper.get("saved_at") else ""
        print(f"{i:2d}. {paper['title']}{star}")
        print(f"    {venue}  ·  {jif}  ·  {paper['published_on']}")
        tags = paper.get("tags") or (
            [paper["top_topic"]] if paper.get("top_topic") else []
        )
        score = paper["score"]
        scored = (
            f"score {score:.3f}  (relevance {paper['relevance']:.2f}, "
            f"impact {paper['impact']:.2f})"
            if score is not None
            else "not scored yet"
        )
        print(f"    {scored}  · {', '.join(tags) if tags else 'no clear topic'}")
        print(f"    {paper['url']}\n")


def cmd_search(args) -> int:
    with db.session() as conn:
        papers, total = db.search_papers(
            conn,
            args.query,
            limit=args.limit,
            order=args.order,
            saved_only=args.saved,
        )
        indexed = db.has_fts(conn)

    if args.json:
        print(json.dumps({"query": args.query, "total": total, "papers": papers}, indent=2))
        return 0

    header = f"{total} match{'' if total == 1 else 'es'} for {args.query!r}"
    print(f"\n{header}\n{'=' * len(header)}")
    if not papers:
        print("\nNothing matched. Search covers titles and abstracts only.\n")
        return 0
    if total > len(papers):
        print(f"showing the first {len(papers)}, ordered by {args.order}\n")
    else:
        print(f"ordered by {args.order}\n")
    if not indexed:
        print("(no full-text index in this SQLite build; scanned instead)\n")
    _print_papers(papers, show_saved=True)
    return 0


def cmd_saved(args) -> int:
    with db.session() as conn:
        papers = db.saved_papers(conn, limit=args.limit)
        total = db.count_saved(conn)

    if args.json:
        print(json.dumps({"total": total, "papers": papers}, indent=2))
        return 0

    header = f"{total} saved paper{'' if total == 1 else 's'}"
    print(f"\n{header}\n{'=' * len(header)}")
    if not papers:
        print("\nNothing saved yet. Press the star on a paper in the web UI.\n")
        return 0
    print()
    _print_papers(papers)
    return 0


def cmd_top(args) -> int:
    from .pipeline import digest

    config = load_config()
    reference = args.date or date.today()
    with db.session() as conn:
        result = digest(
            conn,
            args.window,
            reference,
            args.limit,
            config.scoring.min_relevance,
            config.scoring.padding_by_window,
        )

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    pad = result.get("padding_days", 0)
    span = f"{result['from']} to {result['to']}"
    header = f"Top {result['count']} — {args.window}" + (
        f" +{pad}d back ({span})" if pad else f" ({span})"
    )
    print(f"\n{header}\n{'=' * len(header)}")
    if not result["papers"]:
        print("\nNothing stored for this window yet. Run:  loki fetch --backfill 7\n")
        return 0
    print(f"{result['considered']} papers considered\n")
    _print_papers(result["papers"], show_saved=True)
    return 0


def _server_pid() -> int | None:
    """The PID of a running Loki server, or None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)   # signal 0 only checks that the process exists
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid
    return pid


def cmd_serve(args) -> int:
    import atexit

    import uvicorn

    running = _server_pid()
    if running and not args.reload:
        print(
            f"Loki is already serving (pid {running}). "
            f"Open http://{args.host}:{args.port} or run 'loki stop' first.",
            file=sys.stderr,
        )
        return 1

    ensure_dirs()
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    print(f"Loki is serving on http://{args.host}:{args.port}")
    try:
        uvicorn.run(
            "loki.server:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="debug" if args.verbose else "info",
        )
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_stop(args) -> int:
    """Stop a server started by the app or by 'loki serve'."""
    pid = _server_pid()
    if not pid:
        print("Loki is not running.")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        print(f"Could not stop pid {pid}: {exc}", file=sys.stderr)
        return 1

    for _ in range(40):
        time.sleep(0.25)
        if _server_pid() is None:
            print(f"Stopped Loki (pid {pid}).")
            return 0

    print(f"Loki (pid {pid}) did not stop; sending SIGKILL.", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_status(args) -> int:
    pid = _server_pid()
    if pid:
        print(f"Loki is running (pid {pid}).")
        return 0
    print("Loki is not running.")
    return 1


def cmd_stats(args) -> int:
    with db.session() as conn:
        payload = db.stats(conn)
        payload["embedder"] = db.get_meta(conn, "embedder")
        payload["last_fetch_at"] = db.get_meta(conn, "last_fetch_at")
        payload["runs"] = db.recent_runs(conn, 5)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_doctor(args) -> int:
    """Check that every piece Loki needs is in place."""
    ok = True

    print(f"Loki {__version__}")
    print(f"Python {sys.version.split()[0]}")

    print(f"\nConfig        {INTERESTS_FILE}")
    try:
        config = load_config()
        print(f"  topics      {len(config.topics)}: {', '.join(config.topic_names)}")
        print(
            f"  weights     relevance {config.scoring.relevance_weight} / "
            f"impact {config.scoring.impact_weight}"
        )
    except ConfigError as exc:
        print(f"  BROKEN      {exc}")
        return 1

    from .scoring.impact import JournalIndex

    index = JournalIndex.load()
    print(f"\nJournals      {len(index)} indexed")
    if len(index) == 0:
        print("  WARNING     no impact factors, so every journal scores as unknown")
        ok = False

    print("\nEmbedder")
    try:
        from .scoring.embed import get_embedder

        embedder = get_embedder(config.embedding.backend, config.embedding.model)
        print(f"  backend     {embedder.name} (dim {embedder.dim})")
        if embedder.name.startswith("hashing") and config.embedding.backend != "hashing":
            print("  WARNING     fell back to lexical matching; model2vec is unavailable")
            ok = False
    except Exception as exc:
        print(f"  BROKEN      {exc}")
        return 1

    print(f"\nDatabase      {DB_FILE}")
    with db.session() as conn:
        info = db.stats(conn)
        print(f"  papers      {info['total_papers']}")
        print(f"  by source   {info['by_source'] or 'none yet'}")
        print(f"  span        {info['earliest'] or '-'} to {info['latest'] or '-'}")
        print(f"  saved       {info['saved_papers']}")
        print(
            "  search      "
            + ("full-text index ready" if db.has_fts(conn)
               else "no FTS5 in this SQLite build; search will scan")
        )
        background = db.get_meta(conn, "background_vector")
        count = db.get_meta(conn, "background_count", "0")
        print(f"  background  {'set' if background else 'not set'} (from {count} papers)")
        if info["last_run"]:
            run = info["last_run"]
            print(f"  last run    {run['started_at']} → {run['status']}")
            if run.get("detail"):
                print(f"              {run['detail'][:200]}")

    print("\nNetwork")
    from .sources.http import build_client, get_with_retry

    with build_client(config.pubmed.contact_email) as client:
        for label, url in (
            ("PubMed  ", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi?db=pubmed&retmode=json"),
            ("bioRxiv ", "https://api.biorxiv.org/details/biorxiv/2026-01-01/2026-01-01/0"),
        ):
            resp = get_with_retry(client, url, attempts=1)
            state = f"ok ({resp.status_code})" if resp else "UNREACHABLE"
            if not resp:
                ok = False
            print(f"  {label}    {state}")

    print(f"\n{'All checks passed.' if ok else 'Some checks reported problems (see above).'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loki",
        description="A local daily literature radar for biological research.",
    )
    parser.add_argument("--version", action="version", version=f"loki {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="fetch, score and store new papers")
    p_fetch.add_argument(
        "--backfill",
        type=int,
        default=2,
        metavar="DAYS",
        help="how many days back to cover (default 2)",
    )
    p_fetch.add_argument("--start", type=_parse_day, metavar="YYYY-MM-DD")
    p_fetch.add_argument("--end", type=_parse_day, metavar="YYYY-MM-DD")
    p_fetch.add_argument("--log-file", action="store_true", help="also write data/logs/loki.log")
    p_fetch.set_defaults(func=cmd_fetch)

    p_top = sub.add_parser("top", help="print the top papers for a window")
    p_top.add_argument(
        "window", nargs="?", default="day", choices=("day", "week", "month")
    )
    p_top.add_argument("--limit", type=int, default=10)
    p_top.add_argument("--date", type=_parse_day, metavar="YYYY-MM-DD")
    p_top.add_argument("--json", action="store_true")
    p_top.set_defaults(func=cmd_top)

    p_search = sub.add_parser(
        "search", help="full-text search over every stored title and abstract"
    )
    p_search.add_argument("query", help="words to look for; quote a phrase")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument(
        "--order",
        default="score",
        choices=db.SEARCH_ORDERS,
        help="score (default), match for best text match, or date",
    )
    p_search.add_argument(
        "--saved", action="store_true", help="only search saved papers"
    )
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_saved = sub.add_parser("saved", help="list saved papers, newest save first")
    p_saved.add_argument("--limit", type=int, default=100)
    p_saved.add_argument("--json", action="store_true")
    p_saved.set_defaults(func=cmd_saved)

    p_serve = sub.add_parser("serve", help="run the web server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8713)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_stop = sub.add_parser("stop", help="stop the running server")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="say whether the server is running")
    p_status.set_defaults(func=cmd_status)

    p_rescore = sub.add_parser(
        "rescore", help="recompute scores after editing interests.yaml or journals.json"
    )
    p_rescore.set_defaults(func=cmd_rescore)

    p_prune = sub.add_parser(
        "prune", help="delete stored history older than the digest windows reach"
    )
    p_prune.add_argument(
        "--older-than",
        type=int,
        metavar="DAYS",
        help="override storage.retention_days for this run",
    )
    p_prune.add_argument(
        "--dry-run", action="store_true", help="report what would go, delete nothing"
    )
    p_prune.set_defaults(func=cmd_prune)

    p_stats = sub.add_parser("stats", help="print database statistics as JSON")
    p_stats.set_defaults(func=cmd_stats)

    p_doctor = sub.add_parser("doctor", help="check config, model, database and network")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, to_file=getattr(args, "log_file", False))
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
