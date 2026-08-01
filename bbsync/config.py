"""Paths and user configuration (~/.bbsync/config.toml)."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Default target; any Blackboard Ultra instance works — override with
# `bbsync login --url` or by editing base_url in config.toml.
DEFAULT_BASE_URL = "https://bb.imperial.ac.uk"

BBSYNC_DIR = Path.home() / ".bbsync"
CONFIG_PATH = BBSYNC_DIR / "config.toml"
MANIFEST_PATH = BBSYNC_DIR / "manifest.json"
PROFILE_DIR = BBSYNC_DIR / "browser-profile"
LOG_DIR = BBSYNC_DIR / "logs"

DEFAULT_DEST = Path.home() / "Documents" / "BlackboardNotes"


@dataclass
class CourseEntry:
    name: str
    enabled: bool = True


@dataclass
class Config:
    dest: Path = DEFAULT_DEST
    interval_hours: int = 4
    base_url: str = DEFAULT_BASE_URL
    # for AI page summaries in the dashboard; ANTHROPIC_API_KEY env var wins
    anthropic_api_key: str | None = None
    # keyed by Blackboard course pk id, e.g. "_12345_1"
    courses: dict[str, CourseEntry] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            return cls()
        data = tomllib.loads(CONFIG_PATH.read_text())
        courses = {
            cid: CourseEntry(name=c.get("name", cid), enabled=c.get("enabled", True))
            for cid, c in data.get("courses", {}).items()
        }
        return cls(
            dest=Path(data.get("dest", str(DEFAULT_DEST))).expanduser(),
            interval_hours=int(data.get("interval_hours", 4)),
            base_url=(data.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
            anthropic_api_key=data.get("anthropic_api_key") or None,
            courses=courses,
        )

    def save(self) -> None:
        BBSYNC_DIR.mkdir(parents=True, exist_ok=True)
        # json.dumps produces valid TOML basic strings/keys
        lines = [
            f"dest = {json.dumps(str(self.dest))}",
            f"interval_hours = {self.interval_hours}",
            f"base_url = {json.dumps(self.base_url)}",
        ]
        if self.anthropic_api_key:
            lines.append(f"anthropic_api_key = {json.dumps(self.anthropic_api_key)}")
        lines.append("")
        for cid, course in sorted(self.courses.items(), key=lambda kv: kv[1].name.lower()):
            lines += [
                f"[courses.{json.dumps(cid)}]",
                f"name = {json.dumps(course.name)}",
                f"enabled = {'true' if course.enabled else 'false'}",
                "",
            ]
        CONFIG_PATH.write_text("\n".join(lines))

    def find_course(self, ref: str) -> str | None:
        """Resolve a user-supplied reference (pk id or name substring) to a course key."""
        if ref in self.courses:
            return ref
        matches = [cid for cid, c in self.courses.items() if ref.lower() in c.name.lower()]
        return matches[0] if len(matches) == 1 else None
