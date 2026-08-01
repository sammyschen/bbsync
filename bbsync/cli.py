"""bbsync command-line interface."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

from . import auth, schedule, textindex
from .client import BBClient
from .config import CONFIG_PATH, LOG_DIR, Config
from .manifest import Manifest
from .notify import notify
from .sync import run_sync, update_course_list

log = logging.getLogger("bbsync")


def cmd_login(args) -> int:
    config = Config.load()
    if args.url:
        config.base_url = args.url.rstrip("/")
        config.save()
    print(f"A browser window will open — log in with your {config.base_url} account (SSO + MFA).")
    with auth.browser(headless=False) as ctx:
        user = auth.ensure_session(ctx, config.base_url, interactive=True)
        if not user:
            print("Login did not complete within 5 minutes. Try again with: bbsync login")
            return 1
        name = user.get("userName") or user.get("id")
        print(f"Logged in as {name}. Session saved for future headless runs.\n")

        client = BBClient(ctx, config.base_url)
        update_course_list(client, config, user["id"])

    _print_courses(config)
    print(f"\nAll courses are enabled by default — edit with 'bbsync courses --disable <name>'.")
    print("Next steps:  bbsync sync          (pull files now)")
    print("             bbsync schedule install   (auto-sync in the background)")
    return 0


def cmd_sync(_args) -> int:
    config = Config.load()
    manifest = Manifest.load()
    try:
        with auth.browser(headless=True) as ctx:
            user = auth.ensure_session(ctx, config.base_url)
            if not user:
                log.error("Blackboard session expired — run 'bbsync login' to sign in again.")
                notify("bbsync", "Blackboard session expired — run 'bbsync login' in a terminal.")
                return 2
            client = BBClient(ctx, config.base_url)
            stats = run_sync(client, config, manifest, user["id"])
    except Exception as exc:  # includes a locked browser profile from a concurrent run
        log.error("sync failed: %s", exc)
        return 1

    manifest.mark_synced()
    manifest.save()
    log.info("done — %s", stats.summary())
    if textindex.index_exists():
        try:
            indexed, removed = textindex.update_index(config)
            if indexed or removed:
                log.info("search index: %d file(s) re-indexed, %d removed", indexed, removed)
        except Exception as exc:
            log.warning("search index update failed: %s", exc)
    new = stats.downloaded + stats.updated
    if new:
        notify("bbsync", f"{new} new file{'s' if new != 1 else ''} downloaded from Blackboard")
    return 0


def cmd_courses(args) -> int:
    config = Config.load()
    if not config.courses:
        print("No courses known yet — run 'bbsync login' first.")
        return 1
    for ref, enable in ((args.enable, True), (args.disable, False)):
        if ref:
            cid = config.find_course(ref)
            if not cid:
                print(f"No unique course matches {ref!r}")
                return 1
            config.courses[cid].enabled = enable
            config.save()
    _print_courses(config)
    return 0


def cmd_index(args) -> int:
    config = Config.load()
    if not config.dest.exists():
        print(f"Notes folder {config.dest} doesn't exist — run 'bbsync sync' first.")
        return 1
    indexed, removed = textindex.update_index(config, rebuild=args.rebuild)
    print(f"Indexed {indexed} new/changed file(s), removed {removed}; "
          f"{textindex.doc_count()} documents searchable.")
    return 0


def cmd_search(args) -> int:
    if not textindex.index_exists():
        print("No search index yet — run 'bbsync index' first (one-off, takes a few minutes).")
        return 1
    query = " ".join(args.query)
    try:
        results = textindex.search(query, course=args.course, limit=args.limit)
    except sqlite3.OperationalError as exc:
        print(f"Bad query syntax: {exc}")
        return 1
    if not results:
        print("No matches.")
        return 1

    tty = sys.stdout.isatty()
    bold, dim, reset = ("\x1b[1m", "\x1b[2m", "\x1b[0m") if tty else ("", "", "")
    hl_start, hl_end = ("\x1b[1;33m", "\x1b[0m") if tty else ("»", "«")
    for i, doc in enumerate(results, 1):
        rel = Path(doc["path"])
        label = textindex.PAGE_LABEL.get(rel.suffix.lower(), "p.")
        pages = ", ".join(f"{label}{p}" for p, _ in doc["hits"]) if label else ""
        inside = str(rel.relative_to(doc["course"]))
        print(f"{bold}{i}. {inside}{reset}  {f'({pages})' if pages else ''}")
        print(f"   {dim}{doc['course']}{reset}")
        for page, snip in doc["hits"][:2]:
            clean = " ".join(snip.split()).replace(textindex.HL_START, hl_start).replace(
                textindex.HL_END, hl_end)
            prefix = f"{label}{page}: " if label else ""
            print(f"   {dim}{prefix}{reset}{clean}")
        print()
    return 0


def cmd_schedule(args) -> int:
    config = Config.load()
    if args.action == "install":
        try:
            desc = schedule.install(config.interval_hours)
        except schedule.ScheduleUnsupported as exc:
            print(f"Not supported here: {exc}")
            return 1
        print(f"{desc}. Logs: {LOG_DIR / 'sync.log'}")
    elif args.action == "uninstall":
        print("Removed." if schedule.uninstall() else "No schedule was installed.")
    else:
        line = schedule.status()
        print(f"Loaded: {line}" if line else "Not scheduled.")
    return 0


def cmd_dashboard(args) -> int:
    try:
        import uvicorn
        from .server.app import app
    except ModuleNotFoundError as exc:
        print(f"Dashboard dependencies missing ({exc.name}) — reinstall with: pip install -e .")
        return 1
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    print(f"Dashboard running at {url} — press Ctrl+C to stop.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_status(_args) -> int:
    config = Config.load()
    manifest = Manifest.load()
    enabled = sum(c.enabled for c in config.courses.values())
    print(f"Config:      {CONFIG_PATH}")
    print(f"Destination: {config.dest}")
    print(f"Courses:     {enabled} enabled / {len(config.courses)} known")
    print(f"Last sync:   {manifest.last_sync or 'never'}")
    line = schedule.status()
    print(f"Schedule:    {'every ' + str(config.interval_hours) + 'h (loaded)' if line else 'not installed'}")
    return 0


def _print_courses(config: Config) -> None:
    print(f"Courses ({len(config.courses)}):")
    for cid, c in sorted(config.courses.items(), key=lambda kv: kv[1].name.lower()):
        mark = "[x]" if c.enabled else "[ ]"
        print(f"  {mark} {c.name}   ({cid})")


def main(argv: list[str] | None = None) -> None:
    # On macOS, launchd captures stderr into the log file; on Windows the
    # scheduled task runs under pythonw (no console, sys.stderr is None),
    # so log to the file directly there.
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if sys.platform == "win32":
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_DIR / "sync.log", encoding="utf-8"))
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(message)s", level=logging.INFO, handlers=handlers
    )

    parser = argparse.ArgumentParser(
        prog="bbsync",
        description="Download and organise Blackboard Ultra course files.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="open a browser to sign in once (SSO + MFA)")
    p.add_argument(
        "--url", metavar="URL",
        help="Blackboard Ultra base URL, e.g. https://bb.example.ac.uk "
             "(defaults to bb.imperial.ac.uk on first run; remembered after that)",
    )
    sub.add_parser("sync", help="download new/changed files now")
    p = sub.add_parser("courses", help="list courses; enable/disable syncing per course")
    p.add_argument("--enable", metavar="NAME_OR_ID")
    p.add_argument("--disable", metavar="NAME_OR_ID")
    p = sub.add_parser("index", help="build/update the full-text search index")
    p.add_argument("--rebuild", action="store_true", help="drop and re-index everything")
    p = sub.add_parser("search", help="full-text search across all downloaded notes")
    p.add_argument("query", nargs="+", help="search terms (quote for exact phrases)")
    p.add_argument("-n", "--limit", type=int, default=10, help="max documents shown")
    p.add_argument("--course", metavar="SUBSTR", help="only search courses matching this")
    p = sub.add_parser("schedule", help="manage the background auto-sync")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    sub.add_parser("status", help="show config, last sync and schedule state")
    p = sub.add_parser("dashboard", help="open the web dashboard (everything above, no terminal needed)")
    p.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")

    args = parser.parse_args(argv)
    handler = {
        "login": cmd_login,
        "sync": cmd_sync,
        "courses": cmd_courses,
        "index": cmd_index,
        "search": cmd_search,
        "schedule": cmd_schedule,
        "status": cmd_status,
        "dashboard": cmd_dashboard,
    }[args.cmd]
    sys.exit(handler(args))
