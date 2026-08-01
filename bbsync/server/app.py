"""Local dashboard API. Binds to localhost only; all state lives in ~/.bbsync."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, auth, schedule, textindex
from ..client import BBClient
from ..config import Config
from ..manifest import Manifest
from ..sync import run_sync, update_course_list
from .jobs import Job, runner

log = logging.getLogger("bbsync")

STATIC_DIR = Path(__file__).parent / "static"

# last known session state, refreshed by any job that touches the browser
session_state: dict = {"logged_in": None, "user": None}


# -- job bodies ---------------------------------------------------------------

def _remember_session(user: dict | None) -> None:
    session_state["logged_in"] = bool(user)
    session_state["user"] = (user or {}).get("userName") or (user or {}).get("id")


def _check_session_job(job: Job) -> str:
    log.info("checking Blackboard session…")
    config = Config.load()
    with auth.browser(headless=True) as ctx:
        user = auth.ensure_session(ctx, config.base_url)
    _remember_session(user)
    return "signed in" if user else "signed out"


def _login_job(job: Job) -> str:
    config = Config.load()
    log.info("a browser window is opening — sign in with your %s account", config.base_url)
    with auth.browser(headless=False) as ctx:
        user = auth.ensure_session(ctx, config.base_url, interactive=True)
        _remember_session(user)
        if not user:
            raise RuntimeError("login was not completed — try again")
        log.info("signed in, discovering courses…")
        update_course_list(BBClient(ctx, config.base_url), config, user["id"])
    n = len(Config.load().courses)
    return f"signed in as {session_state['user']} — {n} courses found"


def _sync_job(job: Job) -> str:
    config = Config.load()
    manifest = Manifest.load()
    with auth.browser(headless=True) as ctx:
        user = auth.ensure_session(ctx, config.base_url)
        _remember_session(user)
        if not user:
            raise RuntimeError("Blackboard session expired — sign in again")
        stats = run_sync(BBClient(ctx, config.base_url), config, manifest, user["id"])
    manifest.mark_synced()
    manifest.save()
    if textindex.index_exists():
        indexed, removed = textindex.update_index(config)
        if indexed or removed:
            log.info("search index: %d file(s) re-indexed, %d removed", indexed, removed)
    return stats.summary()


def _make_index_job(rebuild: bool):
    def _index_job(job: Job) -> str:
        indexed, removed = textindex.update_index(Config.load(), rebuild=rebuild)
        return f"{indexed} file(s) indexed, {removed} removed — {textindex.doc_count()} documents searchable"
    return _index_job


# -- app ----------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    runner.start("check-session", _check_session_job)
    yield


app = FastAPI(title="bbsync", version=__version__, lifespan=_lifespan)

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def _localhost_only(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


def _dest() -> Path:
    return Config.load().dest.resolve()


def _safe_path(rel: str) -> Path:
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise HTTPException(400, "bad path")
    dest = _dest()
    p = (dest / rel).resolve()
    if p != dest and not p.is_relative_to(dest):
        raise HTTPException(400, "bad path")
    return p


def _start(kind: str, fn) -> dict:
    job, busy = runner.start(kind, fn)
    if busy:
        raise HTTPException(409, busy)
    return {"started": kind}


# -- status & jobs ------------------------------------------------------------

@app.get("/api/status")
def api_status():
    config = Config.load()
    manifest = Manifest.load()
    return {
        "version": __version__,
        "dest": str(config.dest),
        "base_url": config.base_url,
        "interval_hours": config.interval_hours,
        "session": session_state,
        "last_sync": manifest.last_sync,
        "courses_total": len(config.courses),
        "courses_enabled": sum(c.enabled for c in config.courses.values()),
        "index_docs": textindex.doc_count() if textindex.index_exists() else 0,
        "schedule_installed": schedule.status() is not None,
        "setup_complete": bool(config.courses) and manifest.last_sync is not None,
        "platform": sys.platform,
    }


@app.get("/api/jobs/current")
def api_jobs_current():
    return runner.current.to_dict() if runner.current else {"state": "idle"}


@app.post("/api/login")
def api_login():
    return _start("login", _login_job)


@app.post("/api/sync")
def api_sync():
    return _start("sync", _sync_job)


class IndexBody(BaseModel):
    rebuild: bool = False


@app.post("/api/index")
def api_index(body: IndexBody | None = None):
    return _start("index", _make_index_job(bool(body and body.rebuild)))


@app.post("/api/check-session")
def api_check_session():
    return _start("check-session", _check_session_job)


# -- AI page summaries -----------------------------------------------------------

SUMMARY_MODEL = "claude-haiku-4-5"
SUMMARY_SYSTEM = (
    "A student is scanning full-text search results and needs to decide whether a "
    "page is the one they want, without opening it. You will be given the term(s) "
    "they searched for and the page's text. In one sentence (at most 25 words), say "
    "specifically what appears around that term on this page — the surrounding "
    "topic, equation, definition, or example — so they can judge relevance at a "
    "glance. Do not just restate the search term or give a generic page overview; "
    "anchor the sentence on what's actually near the match. No preamble."
)


@app.get("/api/summary")
def api_summary(path: str, page: int, q: str = ""):
    cached = textindex.get_summary(path, page, q)
    if cached:
        return {"summary": cached}

    text = textindex.page_text(path, page)
    if text is None:
        raise HTTPException(404, "that page isn't in the search index")

    try:
        import anthropic
    except ModuleNotFoundError:
        raise HTTPException(503, "summaries need the 'anthropic' package — reinstall with: pip install -e .")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or Config.load().anthropic_api_key
    if not api_key:
        raise HTTPException(503, "AI summaries are off — add anthropic_api_key to ~/.bbsync/config.toml")

    query_line = f'They searched for: "{q}"\n\n' if q else ""
    try:
        msg = anthropic.Anthropic(api_key=api_key).messages.create(
            model=SUMMARY_MODEL,
            max_tokens=100,
            system=SUMMARY_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"{query_line}{Path(path).name}, page {page}:\n\n{text[:6000]}",
            }],
        )
    except anthropic.APIStatusError as exc:
        raise HTTPException(502, f"summary failed: {exc.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "summary failed: can't reach the Anthropic API")

    summary = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not summary:
        raise HTTPException(502, "summary failed: empty response")
    textindex.store_summary(path, page, q, summary)
    return {"summary": summary}


# -- courses -------------------------------------------------------------------

@app.get("/api/courses")
def api_courses():
    config = Config.load()
    return [
        {"id": cid, "name": c.name, "enabled": c.enabled}
        for cid, c in sorted(config.courses.items(), key=lambda kv: kv[1].name.lower())
    ]


class CourseBody(BaseModel):
    enabled: bool


@app.patch("/api/courses/{cid}")
def api_course_toggle(cid: str, body: CourseBody):
    config = Config.load()
    if cid not in config.courses:
        raise HTTPException(404, "unknown course")
    config.courses[cid].enabled = body.enabled
    config.save()
    return {"id": cid, "enabled": body.enabled}


# -- search & files -------------------------------------------------------------

@app.get("/api/search")
def api_search(q: str, course: str | None = None, n: int = 15):
    if not textindex.index_exists():
        raise HTTPException(409, "no search index yet — run a sync first")
    import sqlite3
    try:
        # longer snippets than the CLI: they feed the dashboard's hover previews
        results = textindex.search(q, course=course, limit=min(n, 50), snippet_tokens=40)
    except sqlite3.OperationalError as exc:
        raise HTTPException(400, f"bad query: {exc}")
    for doc in results:
        suffix = Path(doc["path"]).suffix.lower()
        doc["label"] = textindex.PAGE_LABEL.get(suffix, "p.")
        doc["hits"] = [{"page": p, "snippet": s} for p, s in doc["hits"]]
    return results


@app.get("/api/recent")
def api_recent(n: int = 15):
    dest = _dest()
    manifest = Manifest.load()
    entries = []
    for state in manifest._data["attachments"].values():
        p = Path(state["path"])
        try:
            rel = p.relative_to(dest)
        except ValueError:
            continue
        entries.append({
            "path": str(rel),
            "name": p.name,
            "course": rel.parts[0] if rel.parts else "",
            "downloaded": state.get("downloaded"),
            "exists": p.exists(),
        })
    entries.sort(key=lambda e: e["downloaded"] or "", reverse=True)
    return entries[:n]


@app.get("/api/tree")
def api_tree(path: str = ""):
    target = _safe_path(path) if path else _dest()
    if not target.is_dir():
        raise HTTPException(404, "not a folder")
    dest = _dest()
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        entries.append({
            "name": child.name,
            "dir": child.is_dir(),
            "path": str(child.relative_to(dest)),
            "size": child.stat().st_size if child.is_file() else None,
        })
    return entries


@app.get("/files/{rel:path}")
def api_file(rel: str):
    p = _safe_path(rel)
    if not p.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(p)


class RevealBody(BaseModel):
    path: str


@app.post("/api/reveal")
def api_reveal(body: RevealBody):
    p = _safe_path(body.path)
    if not p.exists():
        raise HTTPException(404, "no such file")
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(p)], capture_output=True)
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(p)], capture_output=True)
    else:
        subprocess.run(["xdg-open", str(p.parent)], capture_output=True)
    return {"ok": True}


# -- config & schedule ----------------------------------------------------------

class ConfigBody(BaseModel):
    interval_hours: int | None = None


@app.patch("/api/config")
def api_config(body: ConfigBody):
    config = Config.load()
    if body.interval_hours is not None:
        if not 1 <= body.interval_hours <= 24:
            raise HTTPException(400, "interval must be 1–24 hours")
        config.interval_hours = body.interval_hours
        config.save()
        if schedule.status() is not None:
            schedule.install(config.interval_hours)
    return {"interval_hours": config.interval_hours}


class ScheduleBody(BaseModel):
    action: str  # install | uninstall


@app.post("/api/schedule")
def api_schedule(body: ScheduleBody):
    config = Config.load()
    if body.action == "install":
        try:
            desc = schedule.install(config.interval_hours)
        except schedule.ScheduleUnsupported as exc:
            raise HTTPException(400, str(exc))
        return {"installed": True, "detail": desc}
    if body.action == "uninstall":
        schedule.uninstall()
        return {"installed": False}
    raise HTTPException(400, "action must be install or uninstall")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
