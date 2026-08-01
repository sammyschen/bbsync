"""Walk course content trees and mirror files into the destination folder."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .client import BBClient, BBError, Forbidden
from .config import Config, CourseEntry
from .manifest import Manifest

log = logging.getLogger("bbsync")

_FOLDER_TYPES = {"resource/x-bb-folder", "resource/x-bb-lesson"}
_FILE_TYPES = {"resource/x-bb-file", "resource/x-bb-document", "resource/x-bb-assignment"}
_LINK_TYPES = {"resource/x-bb-externallink", "resource/x-bb-blti-link"}

_VIDEO_PAT = re.compile(
    r"panopto|echo360|mediasite|kaltura|lecture ?capture|recording|youtube|vimeo|stream",
    re.IGNORECASE,
)

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# names Windows refuses regardless of extension (CON.pdf is as invalid as CON)
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str) -> str:
    s = _ILLEGAL_CHARS.sub("-", name)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s[:150] or "untitled"


@dataclass
class Stats:
    courses: int = 0
    downloaded: int = 0
    updated: int = 0
    skipped: int = 0
    links_added: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"{self.courses} courses: {self.downloaded} new, {self.updated} updated, "
            f"{self.skipped} unchanged, {self.links_added} video links, {self.errors} errors"
        )


def update_course_list(client: BBClient, config: Config, user_id: str) -> None:
    """Add newly discovered enrolments to the config (enabled by default)."""
    for m in client.memberships(user_id):
        course = m.get("course") or {}
        cid = course.get("id") or m.get("courseId")
        if not cid:
            continue
        name = course.get("name") or cid
        if cid not in config.courses:
            config.courses[cid] = CourseEntry(name=name)
            log.info("new course: %s", name)
        else:
            config.courses[cid].name = name
    config.save()


def run_sync(client: BBClient, config: Config, manifest: Manifest, user_id: str) -> Stats:
    stats = Stats()
    update_course_list(client, config, user_id)

    enabled = [(cid, entry) for cid, entry in config.courses.items() if entry.enabled]
    for i, (cid, entry) in enumerate(enabled, 1):
        log.info("[%d/%d] checking %s", i, len(enabled), entry.name)
        try:
            items = client.contents(cid)
        except Forbidden:
            log.warning("no access to %s (unavailable course?) — skipping", entry.name)
            continue
        except BBError as exc:
            log.error("failed to list %s: %s", entry.name, exc)
            stats.errors += 1
            continue
        stats.courses += 1
        course_dir = config.dest / sanitize(entry.name)
        _walk(client, cid, entry.name, items, course_dir, course_dir, manifest, stats, set())
        manifest.save()

    return stats


def _walk(
    client: BBClient,
    course_id: str,
    course_name: str,
    items: list[dict],
    dir_path: Path,
    course_dir: Path,
    manifest: Manifest,
    stats: Stats,
    claimed: set[str],
) -> None:
    for item in items:
        handler = (item.get("contentHandler") or {}).get("id", "")
        title = item.get("title") or "Untitled"

        if handler in _FILE_TYPES:
            _download_attachments(client, course_id, item, title, dir_path, manifest, stats, claimed)
        elif handler in _LINK_TYPES:
            _record_link(client.base_url, course_id, course_name, item, title,
                         dir_path, course_dir, manifest, stats)

        if item.get("hasChildren"):
            try:
                children = client.children(course_id, item["id"])
            except (Forbidden, BBError) as exc:
                log.warning("cannot open '%s' in %s: %s", title, course_name, exc)
                stats.errors += 1
                continue
            sub_dir = dir_path if handler in _FILE_TYPES else dir_path / sanitize(title)
            _walk(client, course_id, course_name, children, sub_dir, course_dir,
                  manifest, stats, claimed)


def _cached_attachment_ok(manifest: Manifest, att_id: str, claimed: set[str]) -> bool:
    state = manifest.attachment(att_id)
    if not state or not Path(state["path"]).exists():
        return False
    claimed.add(state["path"])
    return True


def _download_attachments(
    client: BBClient,
    course_id: str,
    item: dict,
    title: str,
    dir_path: Path,
    manifest: Manifest,
    stats: Stats,
    claimed: set[str],
) -> None:
    item_id = item["id"]
    modified = item.get("modified")

    cached = manifest.item(item_id)
    if cached and cached.get("modified") == modified:
        cached_ids = cached.get("attachments", [])
        if all(_cached_attachment_ok(manifest, aid, claimed) for aid in cached_ids):
            stats.skipped += len(cached_ids)
            return

    try:
        attachments = client.attachments(course_id, item_id)
    except (Forbidden, BBError) as exc:
        log.debug("no attachments for '%s': %s", title, exc)
        return

    seen_ids = []
    for att in attachments:
        att_id = str(att.get("id"))
        seen_ids.append(att_id)
        fname = sanitize(att.get("fileName") or att_id)
        state = manifest.attachment(att_id)

        target = dir_path / fname
        if str(target) in claimed and (state is None or state.get("path") != str(target)):
            target = dir_path / f"{sanitize(title)} - {fname}"
        claimed.add(str(target))

        if (
            state
            and state.get("modified") == modified
            and Path(state["path"]).exists()
        ):
            stats.skipped += 1
            continue

        try:
            client.download(course_id, item["id"], att_id, target)
        except BBError as exc:
            log.error("download failed for %s: %s", fname, exc)
            stats.errors += 1
            continue

        if state:
            stats.updated += 1
            log.info("updated  %s", target)
        else:
            stats.downloaded += 1
            log.info("download %s", target)
        manifest.record_attachment(att_id, str(target), modified)

    manifest.record_item(item_id, modified, seen_ids)


def _record_link(
    base_url: str,
    course_id: str,
    course_name: str,
    item: dict,
    title: str,
    dir_path: Path,
    course_dir: Path,
    manifest: Manifest,
    stats: Stats,
) -> None:
    url = (item.get("contentHandler") or {}).get("url") or (
        f"{base_url}/webapps/blackboard/execute/blti/launchLink"
        f"?course_id={course_id}&content_id={item['id']}"
    )
    if not _VIDEO_PAT.search(f"{title} {url}"):
        return
    key = f"{course_id}:{item['id']}"
    if manifest.has_link(key):
        return

    videos_md = course_dir / "videos.md"
    course_dir.mkdir(parents=True, exist_ok=True)
    if not videos_md.exists():
        videos_md.write_text(f"# Videos & recordings — {course_name}\n\n")
    location = str(dir_path.relative_to(course_dir)) if dir_path != course_dir else ""
    suffix = f" (in {location})" if location and location != "." else ""
    with videos_md.open("a") as f:
        f.write(f"- [{title}]({url}){suffix} — found {date.today().isoformat()}\n")
    manifest.record_link(key, url, title)
    stats.links_added += 1
    log.info("video link: %s (%s)", title, course_name)
