"""Thin wrapper over Blackboard's REST API, authenticated by browser cookies."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from playwright.sync_api import BrowserContext

log = logging.getLogger("bbsync")

# polite delay between requests, seconds
_REQUEST_DELAY = 0.023


class BBError(Exception):
    pass


class AuthExpired(BBError):
    pass


class Forbidden(BBError):
    pass


class BBClient:
    def __init__(self, ctx: BrowserContext, base_url: str):
        self._req = ctx.request
        self.base_url = base_url
        # Flipped to the private SPA API if this deployment rejects cookie
        # auth on the public endpoints.
        self._public = True

    def _path(self, path: str) -> str:
        if not self._public:
            return path.replace("/learn/api/public/v1/", "/learn/api/v1/")
        return path

    def _get(self, path: str):
        time.sleep(_REQUEST_DELAY)
        resp = self._req.get(self.base_url + self._path(path))
        if resp.status == 401 and self._public and "/learn/api/public/v1/" in path:
            log.info("public API rejected session cookies; falling back to private API")
            self._public = False
            resp = self._req.get(self.base_url + self._path(path))
        if resp.status == 401:
            raise AuthExpired("Blackboard session rejected (401)")
        if resp.status == 403:
            raise Forbidden(f"access denied: {path}")
        if not resp.ok:
            raise BBError(f"GET {path} -> {resp.status}")
        return resp

    def _paged(self, path: str) -> list[dict]:
        results: list[dict] = []
        while path:
            data = self._get(path).json()
            results.extend(data.get("results", []))
            path = (data.get("paging") or {}).get("nextPage")
        return results

    def memberships(self, user_id: str) -> list[dict]:
        return self._paged(f"/learn/api/public/v1/users/{user_id}/courses?expand=course")

    def contents(self, course_id: str) -> list[dict]:
        return self._paged(f"/learn/api/public/v1/courses/{course_id}/contents")

    def children(self, course_id: str, content_id: str) -> list[dict]:
        return self._paged(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}/children"
        )

    def attachments(self, course_id: str, content_id: str) -> list[dict]:
        return self._paged(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments"
        )

    def download(self, course_id: str, content_id: str, attachment_id: str, dest: Path) -> None:
        resp = self._get(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}"
            f"/attachments/{attachment_id}/download"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(resp.body())
        part.replace(dest)
