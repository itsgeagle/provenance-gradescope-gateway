"""Authenticated Gradescope client. The ONLY module that talks to Gradescope.

All fragility of the undocumented Gradescope surface lives here. See the live-spike
note in the plan: verify login/export specifics against the real site and pin HTML
fixtures before trusting this in production.
"""

from __future__ import annotations

import httpx

from .parse import Assignment, parse_assignments, parse_csrf_token


class GradescopeError(Exception):
    """A Gradescope request failed or returned an unexpected shape."""


class GradescopeClient:
    def __init__(self, http: httpx.Client, *, base_url: str = "https://www.gradescope.com") -> None:
        self._http = http
        self._base = base_url.rstrip("/")

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
        return parse_assignments(resp.text)

    def download_export(self, course_id: str, assignment_id: str) -> bytes:
        url = (
            f"{self._base}/courses/{course_id}/assignments/{assignment_id}"
            "/export/without_evaluations"
        )
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise GradescopeError(f"export download failed: {e}") from e
        if resp.status_code != 200:
            raise GradescopeError(f"export returned {resp.status_code}")
        return resp.content
