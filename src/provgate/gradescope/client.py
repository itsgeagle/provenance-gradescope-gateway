"""Authenticated Gradescope client. The ONLY module that talks to Gradescope.

All fragility of the undocumented Gradescope surface lives here. The submission
export is an async flow (create a bulk export → poll a generated-file status →
download a presigned S3 ZIP); assignment listing reads a React component's props.
Both were verified against live Gradescope; see the @pytest.mark.live spike in
tests/gradescope/test_export_live.py and the design spec.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx

from .parse import Assignment, parse_assignments, parse_csrf_token

_CSRF_META_RE = re.compile(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"')
# Poll statuses that mean "still generating" (anything else terminal & != completed is an error).
_PENDING_STATUSES = frozenset({"processing", "pending", "queued", "in_progress"})


class GradescopeError(Exception):
    """A Gradescope request failed or returned an unexpected shape."""


class GradescopeClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        base_url: str = "https://www.gradescope.com",
        poll_interval_s: float = 5.0,
        poll_timeout_s: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Precondition: `http` must be constructed with `follow_redirects=True` — `login`
        detects failure by inspecting the post-redirect URL, and the assignment page
        (for the CSRF token) redirects to review_grades."""
        self._http = http
        self._base = base_url.rstrip("/")
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self._sleep = sleep
        self._monotonic = monotonic

    def login(self, email: str, password: str) -> None:
        try:
            page = self._http.get(f"{self._base}/login")
            token = parse_csrf_token(page.text)
            resp = self._http.post(
                f"{self._base}/login",
                data={
                    "utf8": "✓",
                    "authenticity_token": token,
                    "session[email]": email,
                    "session[password]": password,
                    "session[remember_me]": "0",
                    "commit": "Log In",
                },
            )
        except (httpx.HTTPError, ValueError) as e:
            raise GradescopeError(f"login failed: {e}") from e
        # A successful login redirects away from /login; landing back on /login means bad creds.
        if resp.status_code >= 400 or resp.url.path.rstrip("/") == "/login":
            raise GradescopeError("login rejected (check credentials)")

    def list_assignments(self, course_id: str) -> list[Assignment]:
        try:
            resp = self._http.get(f"{self._base}/courses/{course_id}/assignments")
        except httpx.HTTPError as e:
            raise GradescopeError(f"listing assignments failed: {e}") from e
        if resp.status_code != 200:
            raise GradescopeError(f"course page returned {resp.status_code}")
        try:
            return parse_assignments(resp.text)
        except ValueError as e:
            raise GradescopeError(f"could not parse assignments: {e}") from e

    # -- submission export: create -> poll -> download (async) --------------------

    def _csrf_token(self, course_id: str, assignment_id: str) -> str:
        url = f"{self._base}/courses/{course_id}/assignments/{assignment_id}"
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise GradescopeError(f"assignment page fetch failed: {e}") from e
        m = _CSRF_META_RE.search(resp.text)
        if not m:
            raise GradescopeError("no csrf-token meta on assignment page")
        return m.group(1)

    def _create_export(self, course_id: str, assignment_id: str, csrf: str) -> int:
        url = f"{self._base}/courses/{course_id}/assignments/{assignment_id}/export"
        try:
            resp = self._http.post(
                url,
                headers={
                    "X-CSRF-Token": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as e:
            raise GradescopeError(f"export create failed: {e}") from e
        if resp.status_code != 200:
            raise GradescopeError(f"export create returned {resp.status_code}")
        try:
            gfid = resp.json().get("generated_file_id")
        except ValueError as e:
            raise GradescopeError(f"export create returned non-JSON: {e}") from e
        if not gfid:
            raise GradescopeError("export create response missing generated_file_id")
        return int(gfid)

    def _poll_generated_file(self, course_id: str, generated_file_id: int) -> str:
        url = f"{self._base}/courses/{course_id}/generated_files/{generated_file_id}.json"
        deadline = self._monotonic() + self._poll_timeout_s
        while True:
            try:
                resp = self._http.get(url, headers={"Accept": "application/json"})
            except httpx.HTTPError as e:
                raise GradescopeError(f"export status poll failed: {e}") from e
            if resp.status_code != 200:
                raise GradescopeError(f"export status returned {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as e:
                raise GradescopeError(f"export status returned non-JSON: {e}") from e
            status = data.get("status")
            if status == "completed":
                s3_url = data.get("url")
                if not isinstance(s3_url, str) or not s3_url:
                    raise GradescopeError("completed export missing download url")
                return s3_url
            if status not in _PENDING_STATUSES:
                raise GradescopeError(f"export generation failed (status {status!r})")
            if self._monotonic() >= deadline:
                raise GradescopeError("export generation did not complete within timeout")
            self._sleep(self._poll_interval_s)

    def _download_to_temp(self, url: str) -> Path:
        fd, name = tempfile.mkstemp(prefix="provgate-export-", suffix=".zip")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh, self._http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise GradescopeError(f"export download returned {resp.status_code}")
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        except (httpx.HTTPError, GradescopeError):
            path.unlink(missing_ok=True)
            raise
        return path

    @contextlib.contextmanager
    def download_export(self, course_id: str, assignment_id: str) -> Iterator[Path]:
        """Create the submission export, poll it to completion, stream the S3 ZIP to a
        temp file, and yield its path. The temp file is deleted on context exit — the
        full export never enters memory and is never retained beyond the run."""
        csrf = self._csrf_token(course_id, assignment_id)
        generated_file_id = self._create_export(course_id, assignment_id, csrf)
        s3_url = self._poll_generated_file(course_id, generated_file_id)
        path = self._download_to_temp(s3_url)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    def close(self) -> None:
        self._http.close()
