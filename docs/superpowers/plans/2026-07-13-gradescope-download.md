# Gradescope Download + Assignment Listing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Gradescope client work against current (React-rendered) Gradescope: replace the broken `download_export` with the real async create→poll→S3-download flow (streaming through a temp file), and fix `parse_assignments` to read the React `AssignmentsTable` props.

**Architecture:** All Gradescope fragility stays in `provgate/gradescope/`. `download_export` becomes a context manager yielding a temp-file `Path` (the full export never in RAM); `prune_export` gains a `Path` source so only the small delta is buffered; the engine is rewired to the temp-file pipeline; the delta still uploads via the existing chunked path. Verified live during design.

**Tech Stack:** Python 3.11+, `httpx` (sync client, streaming), `respx` for HTTP mocking, `pytest`, `mypy --strict`, `ruff`, `uv`.

**Reference spec:** `docs/superpowers/specs/2026-07-13-gradescope-download-design.md`

## Global Constraints

- Python 3.11+. `mypy --strict` clean — no new `Any` except at the HTML/JSON scraping boundary (`resp.json()`, `resp.text` parsing), matching the existing pattern.
- `ruff check` and `ruff format --check` clean.
- The client is hand-rolled on `httpx`; do NOT add `gradescopeapi` or any dependency.
- Secrets never logged: password, session cookies, `X-CSRF-Token`, and the presigned S3 URL (its `X-Amz-Signature` query string is a bearer capability). No `Authorization`/`Cookie` values logged. This plan adds no logging.
- Student source is never retained: the export temp file is deleted after each assignment (the download-export context manager's `finally`).
- The delta/watermark invariant is unchanged: `prune_export` copies `submission_metadata.yml` verbatim and includes only new `submission_*` folders; the watermark advances only after the Provenance job is `succeeded`/`partial` (engine, unchanged).
- Do NOT modify `provgate/provenance/`, `provgate/store/`, or `provgate/notify/`. Do NOT change the Provenance monorepo.
- Observed Gradescope contract (fixed for this work): create `POST /courses/{cid}/assignments/{aid}/export` (headers: session cookies, `X-CSRF-Token`, `X-Requested-With: XMLHttpRequest`) → `200 {"generated_file_id": <int>}`; poll `GET /courses/{cid}/generated_files/{id}.json` → `{progress (0.0–1.0), status, url, expires_at}`, ready when `status=="completed"`; download the status `url` (presigned S3, `application/zip`). Assignment list: `data-react-class="AssignmentsTable" data-react-props="{escaped JSON}"` with `table_data[].id` (`"assignment_<n>"`) and `.title`.
- Commands run under `uv`.

---

### Task 1: Fix `parse_assignments` for the React `AssignmentsTable`

**Files:**
- Modify: `src/provgate/gradescope/parse.py` (`parse_assignments` at lines 43-48; add `html`/`json`/regex)
- Test: `tests/gradescope/test_parse.py` (replace the anchor-based `COURSE_HTML` + `test_parse_assignments`)

**Interfaces:**
- Produces: `parse_assignments(html: str) -> list[Assignment]` unchanged signature; now parses the React props; raises `ValueError` on missing component / bad JSON / no rows (never returns `[]` silently). `Assignment(id, title)` unchanged.

- [ ] **Step 1: Write the failing tests**

Replace `COURSE_HTML` and `test_parse_assignments` in `tests/gradescope/test_parse.py` with:

```python
import html as _html
import json

import pytest

from provgate.gradescope.parse import Assignment, parse_assignments, parse_csrf_token


def _assignments_page(rows: list[dict]) -> str:
    props = _html.escape(json.dumps({"table_data": rows}), quote=True)
    return f'<div data-react-class="AssignmentsTable" data-react-props="{props}"></div>'


def test_parse_assignments_from_react_props() -> None:
    html = _assignments_page(
        [
            {"id": "assignment_872677", "title": "Homework 1", "url": "/courses/1/assignments/872677"},
            {"id": "assignment_872690", "title": "Homework 2", "url": "/courses/1/assignments/872690"},
        ]
    )
    got = parse_assignments(html)
    assert Assignment(id="872677", title="Homework 1") in got
    assert Assignment(id="872690", title="Homework 2") in got
    assert len(got) == 2


def test_parse_assignments_no_component_raises() -> None:
    with pytest.raises(ValueError):
        parse_assignments("<html><body>no table here</body></html>")


def test_parse_assignments_empty_table_raises() -> None:
    with pytest.raises(ValueError):
        parse_assignments(_assignments_page([]))
```

Keep the existing `LOGIN_HTML` and `test_parse_csrf_token` as-is.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/gradescope/test_parse.py -v`
Expected: FAIL — the current anchor-based `parse_assignments` returns `[]` for the React HTML (assertion failure), and does not raise on missing component.

- [ ] **Step 3: Rewrite `parse_assignments`**

In `src/provgate/gradescope/parse.py`, add imports at the top (after `import re`):

```python
import html as _html
import json
```

Replace the `_ASSIGNMENT_RE` line and the `parse_assignments` function with:

```python
_ASSIGNMENTS_TABLE_RE = re.compile(
    r'data-react-class="AssignmentsTable"\s+data-react-props="([^"]*)"'
)


def parse_assignments(html: str) -> list[Assignment]:
    """Extract assignments from the instructor course page's React `AssignmentsTable`
    component props. Raises ValueError (never returns []) so a markup change surfaces
    loudly instead of silently syncing nothing."""
    m = _ASSIGNMENTS_TABLE_RE.search(html)
    if not m:
        raise ValueError("no AssignmentsTable component on course page")
    try:
        props = json.loads(_html.unescape(m.group(1)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"could not parse AssignmentsTable props: {e}") from e
    rows = props.get("table_data") if isinstance(props, dict) else None
    if not isinstance(rows, list):
        raise ValueError("AssignmentsTable props missing table_data")
    seen: set[str] = set()
    out: list[Assignment] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not isinstance(rid, str):
            continue
        aid = rid[len("assignment_") :] if rid.startswith("assignment_") else rid
        title = row.get("title")
        if aid and aid not in seen:
            seen.add(aid)
            out.append(Assignment(id=aid, title=(title if isinstance(title, str) else "").strip()))
    if not out:
        raise ValueError("AssignmentsTable had no assignment rows")
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gradescope/test_parse.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/provgate/gradescope/parse.py tests/gradescope/test_parse.py && uv run ruff format --check src/provgate/gradescope/parse.py tests/gradescope/test_parse.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/provgate/gradescope/parse.py tests/gradescope/test_parse.py
git commit -m "fix(gradescope): parse assignments from React AssignmentsTable props"
```

---

### Task 2: `prune_export` accepts a file `Path` source

**Files:**
- Modify: `src/provgate/sync/prune.py` (`prune_export` at lines 52-54)
- Test: `tests/sync/test_prune.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `prune_export(source: Path | bytes, already_forwarded: set[str]) -> PrunedExport`. When `source` is bytes, behaves exactly as today; when a `Path`/str, `zipfile.ZipFile` streams from disk (full archive never loaded into RAM). Output `PrunedExport` unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/sync/test_prune.py`:

```python
from pathlib import Path


def test_prune_accepts_a_file_path_source(tmp_path: Path) -> None:
    export = make_export(
        {
            "submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}},
            "submission_2": {"sid": "s2", "files": {"manifest.json": b"b"}},
        }
    )
    path = tmp_path / "export.zip"
    path.write_bytes(export)

    pruned = prune_export(path, already_forwarded={"submission_1"})

    assert pruned.forwarded_keys == frozenset({"submission_2"})
    assert pruned.total_submissions == 2
    names = _names(pruned.zip_bytes)
    assert "assignment_export/submission_metadata.yml" in names
    assert "assignment_export/submission_2/manifest.json" in names
    assert not any("submission_1/" in n for n in names)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sync/test_prune.py::test_prune_accepts_a_file_path_source -v`
Expected: FAIL — `prune_export` calls `io.BytesIO(zip_bytes)` on a `Path`, raising `TypeError`.

- [ ] **Step 3: Accept a path source**

In `src/provgate/sync/prune.py`, add `from pathlib import Path` to the imports, then replace the signature and the opening lines of `prune_export`:

```python
def prune_export(source: Path | bytes, already_forwarded: set[str]) -> PrunedExport:
    src: Path | io.BytesIO = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        zin = zipfile.ZipFile(src)
    except zipfile.BadZipFile as e:
        raise NotAnExportError("not a valid ZIP") from e
```

(The rest of the function body is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/sync/test_prune.py -v`
Expected: PASS (the new path test plus all existing byte-based tests — back-compat).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/provgate/sync/prune.py tests/sync/test_prune.py && uv run ruff format --check src/provgate/sync/prune.py tests/sync/test_prune.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/provgate/sync/prune.py tests/sync/test_prune.py
git commit -m "feat(prune): accept a file Path source so large exports stream from disk"
```

---

### Task 3: Config settings for export poll cadence

**Files:**
- Modify: `src/provgate/config.py` (Settings dataclass + `load_settings`; `_optional_float` already exists at lines 33-37)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.gs_export_poll_interval_s: float` (default `5.0`), `Settings.gs_export_poll_timeout_s: float` (default `600.0`), from `PROVGATE_GS_EXPORT_POLL_INTERVAL_S` / `PROVGATE_GS_EXPORT_POLL_TIMEOUT_S`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_gs_export_poll_settings_default_and_override() -> None:
    base = {"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"}
    s = load_settings(base)
    assert s.gs_export_poll_interval_s == 5.0
    assert s.gs_export_poll_timeout_s == 600.0
    s2 = load_settings(
        {
            **base,
            "PROVGATE_GS_EXPORT_POLL_INTERVAL_S": "2",
            "PROVGATE_GS_EXPORT_POLL_TIMEOUT_S": "120",
        }
    )
    assert s2.gs_export_poll_interval_s == 2.0
    assert s2.gs_export_poll_timeout_s == 120.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_gs_export_poll_settings_default_and_override -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'gs_export_poll_interval_s'`

- [ ] **Step 3: Add the fields**

In `src/provgate/config.py`, add to the `Settings` dataclass (after the existing chunk settings):

```python
    gs_export_poll_interval_s: float = 5.0
    gs_export_poll_timeout_s: float = 600.0
```

And in `load_settings`, inside the `Settings(...)` constructor:

```python
        gs_export_poll_interval_s=_optional_float(env, "PROVGATE_GS_EXPORT_POLL_INTERVAL_S", 5.0),
        gs_export_poll_timeout_s=_optional_float(env, "PROVGATE_GS_EXPORT_POLL_TIMEOUT_S", 600.0),
```

(Defaults must match the dataclass defaults — see the "Fallbacks below must track the Settings dataclass defaults" comment.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/provgate/config.py tests/test_config.py && uv run ruff format --check src/provgate/config.py tests/test_config.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/provgate/config.py tests/test_config.py
git commit -m "feat(config): add Gradescope export poll interval + timeout settings"
```

---

### Task 4: Gradescope client — real async export download flow

**Files:**
- Modify: `src/provgate/gradescope/client.py` (constructor + replace `download_export`; wrap `list_assignments` parse errors)
- Test: `tests/gradescope/test_client.py`

**Interfaces:**
- Consumes: `parse_assignments` raising `ValueError` (Task 1); poll settings passed as constructor kwargs (defaults match Task 3).
- Produces: `GradescopeClient.__init__(http, *, base_url=..., poll_interval_s=5.0, poll_timeout_s=600.0, sleep=time.sleep, monotonic=time.monotonic)`; `download_export(course_id, assignment_id) -> AbstractContextManager[Path]` (yields a temp-file path, deletes it on exit); private `_csrf_token`, `_create_export`, `_poll_generated_file`, `_download_to_temp`. `list_assignments` now raises `GradescopeError` (not `ValueError`) on parse failure. `login`/`close` unchanged.

- [ ] **Step 1: Write the failing tests**

Replace the whole `tests/gradescope/test_client.py` with:

```python
import re
from pathlib import Path

import httpx
import pytest
import respx

from provgate.gradescope.client import GradescopeClient, GradescopeError

GS = "https://www.gradescope.com"
S3 = "https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/uploads/generated_file/file/42/submissions.zip?sig=x"


def make_client(**kw: object) -> GradescopeClient:
    ticks = iter(range(0, 100_000))
    return GradescopeClient(
        httpx.Client(follow_redirects=True),
        base_url=GS,
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
        **kw,  # type: ignore[arg-type]
    )


@respx.mock
def test_login_posts_csrf_and_credentials() -> None:
    respx.get(f"{GS}/login").mock(
        return_value=httpx.Response(200, text='<input name="authenticity_token" value="TOK-1" />')
    )
    login = respx.post(f"{GS}/login").mock(
        return_value=httpx.Response(302, headers={"location": "/account"})
    )
    respx.get(f"{GS}/account").mock(return_value=httpx.Response(200, text="Account"))
    make_client().login("staff@example.edu", "pw")
    assert "TOK-1" in login.calls.last.request.content.decode()


def _mock_assignment_page() -> None:
    respx.get(f"{GS}/courses/1/assignments/2").mock(
        return_value=httpx.Response(200, text='<meta name="csrf-token" content="CSRF-1" />')
    )


@respx.mock
def test_download_export_create_poll_download() -> None:
    _mock_assignment_page()
    create = respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        side_effect=[
            httpx.Response(200, json={"progress": 0.0, "status": "processing", "url": S3}),
            httpx.Response(200, json={"progress": 1.0, "status": "completed", "url": S3}),
        ]
    )
    respx.get(url=S3).mock(return_value=httpx.Response(200, content=b"PK-zip-bytes"))

    with make_client().download_export("1", "2") as path:
        assert isinstance(path, Path)
        assert path.read_bytes() == b"PK-zip-bytes"
        held = path
    # deleted on context exit
    assert not held.exists()
    # create POST carried the CSRF header
    assert create.calls.last.request.headers["x-csrf-token"] == "CSRF-1"


@respx.mock
def test_download_export_ignores_url_until_completed() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    # url present while still processing must NOT trigger download
    status = respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        side_effect=[
            httpx.Response(200, json={"progress": 0.5, "status": "processing", "url": S3}),
            httpx.Response(200, json={"progress": 1.0, "status": "completed", "url": S3}),
        ]
    )
    dl = respx.get(url=S3).mock(return_value=httpx.Response(200, content=b"z"))
    with make_client().download_export("1", "2"):
        pass
    assert status.call_count == 2  # polled twice
    assert dl.call_count == 1  # downloaded once, after completed


@respx.mock
def test_download_export_failed_status_raises() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        return_value=httpx.Response(200, json={"progress": 0.0, "status": "failed", "url": None})
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_download_export_poll_timeout_raises() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        return_value=httpx.Response(200, json={"progress": 0.1, "status": "processing", "url": S3})
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_download_export_missing_csrf_raises() -> None:
    respx.get(f"{GS}/courses/1/assignments/2").mock(
        return_value=httpx.Response(200, text="<html>no meta</html>")
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_list_assignments_wraps_parse_error_as_gradescope_error() -> None:
    respx.get(f"{GS}/courses/1/assignments").mock(
        return_value=httpx.Response(200, text="<html>no react table</html>")
    )
    with pytest.raises(GradescopeError):
        make_client().list_assignments("1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/gradescope/test_client.py -v`
Expected: FAIL — `download_export` is still the old single-GET returning bytes (no context manager, `TypeError`/attribute mismatches); `make_client` passes unknown kwargs.

- [ ] **Step 3: Rewrite `client.py`**

Replace the entire contents of `src/provgate/gradescope/client.py` with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gradescope/test_client.py -v`
Expected: PASS (all, including the preserved login test)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/provgate/gradescope/client.py tests/gradescope/test_client.py && uv run ruff format --check src/provgate/gradescope/client.py tests/gradescope/test_client.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/provgate/gradescope/client.py tests/gradescope/test_client.py
git commit -m "feat(gradescope): real async submission-export download (create/poll/S3, streaming)"
```

---

### Task 5: Port + engine rewiring for the temp-file pipeline

**Files:**
- Modify: `src/provgate/sync/ports.py` (line 14), `src/provgate/sync/engine.py` (`_sync_assignment` lines 58-62)
- Test: `tests/sync/test_engine.py` (`FakeGs`)

**Interfaces:**
- Consumes: `GradescopeClient.download_export` context manager (Task 4); `prune_export(Path | bytes, ...)` (Task 2).
- Produces: `GradescopePort.download_export(course_id, assignment_id) -> AbstractContextManager[Path]`; engine reads the export inside a `with` block.

- [ ] **Step 1: Update the fake and run the engine tests to confirm they fail**

In `tests/sync/test_engine.py`, add imports at the top:

```python
import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path
```

Replace the `FakeGs.download_export` method (lines 35-36) with a context manager that writes the fixture bytes to a temp file (call sites `FakeGs(export)` stay unchanged):

```python
    @contextlib.contextmanager
    def download_export(self, course_id: str, assignment_id: str) -> Iterator[Path]:
        fd, name = tempfile.mkstemp(suffix=".zip")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(self._export)
            yield path
        finally:
            path.unlink(missing_ok=True)
```

Add `import os` to the test file's imports as well.

Run: `uv run pytest tests/sync/test_engine.py -v`
Expected: FAIL — the engine still calls `gs.download_export(...)` expecting `bytes` and passes the context-manager object to `prune_export`, raising an error.

- [ ] **Step 2: Update the `GradescopePort` protocol**

In `src/provgate/sync/ports.py`, add the import and change the method:

```python
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
```

```python
class GradescopePort(Protocol):
    def list_assignments(self, course_id: str) -> list[Assignment]: ...
    def download_export(
        self, course_id: str, assignment_id: str
    ) -> AbstractContextManager[Path]: ...
    def close(self) -> None: ...
```

- [ ] **Step 3: Rewire `_sync_assignment`**

In `src/provgate/sync/engine.py`, replace lines 58-62:

```python
    started = now_iso()
    export = gs.download_export(cfg.gradescope_course_id, aid)
    already = repo.forwarded_keys(cfg.id, aid)
    pruned = prune_export(export, already)
    delta = len(pruned.forwarded_keys)
```

with:

```python
    started = now_iso()
    already = repo.forwarded_keys(cfg.id, aid)
    with gs.download_export(cfg.gradescope_course_id, aid) as export_path:
        pruned = prune_export(export_path, already)
    delta = len(pruned.forwarded_keys)
```

- [ ] **Step 4: Run the engine tests to verify they pass**

Run: `uv run pytest tests/sync/test_engine.py -v`
Expected: PASS (delta-only forwarding, failed-job watermark, dry-run, isolation — all green through the context-manager interface).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/provgate/sync/ports.py src/provgate/sync/engine.py tests/sync/test_engine.py && uv run ruff format --check src/provgate/sync/ports.py src/provgate/sync/engine.py tests/sync/test_engine.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/provgate/sync/ports.py src/provgate/sync/engine.py tests/sync/test_engine.py
git commit -m "refactor(sync): download export to a temp file; prune from disk"
```

---

### Task 6: Wire export poll settings through `real_gs_login`

**Files:**
- Modify: `src/provgate/cli/wiring.py` (`real_gs_login` at lines 22-28), `src/provgate/cli/main.py` (call sites at lines 134, 179)
- Test: `tests/cli/test_wiring.py`

**Interfaces:**
- Consumes: `Settings.gs_export_poll_interval_s`/`gs_export_poll_timeout_s` (Task 3); `GradescopeClient` poll kwargs (Task 4).
- Produces: `real_gs_login(settings: Settings) -> GradescopeLogin` (was `real_gs_login(http_timeout_s: float)`), building the client with `follow_redirects=True` and the poll settings.

- [ ] **Step 1: Write the failing test**

Add to `tests/cli/test_wiring.py` (create the file if Task-2-of-the-chunked-plan already created it; otherwise the imports at top mirror that file):

```python
from pathlib import Path

import httpx
import respx

from provgate.cli.wiring import real_gs_login
from provgate.config import Settings

GS = "https://www.gradescope.com"


@respx.mock
def test_real_gs_login_builds_client_with_poll_settings() -> None:
    # Mock the login round-trip so the factory's login() succeeds without network.
    respx.get(f"{GS}/login").mock(
        return_value=httpx.Response(200, text='<input name="authenticity_token" value="T" />')
    )
    respx.post(f"{GS}/login").mock(
        return_value=httpx.Response(302, headers={"location": "/account"})
    )
    respx.get(f"{GS}/account").mock(return_value=httpx.Response(200, text="ok"))

    settings = Settings(
        db_path=Path("/tmp/x.db"),
        secret_key="k",
        gs_export_poll_interval_s=1.5,
        gs_export_poll_timeout_s=90.0,
    )
    client = real_gs_login(settings)("staff@x.edu", "pw")
    # The constructed client carries the poll settings from config (white-box wiring check).
    assert client._poll_interval_s == 1.5
    assert client._poll_timeout_s == 90.0
    client.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_wiring.py::test_real_gs_login_builds_client_with_poll_settings -v`
Expected: FAIL — `real_gs_login` currently takes `http_timeout_s: float`, so passing `Settings` and the new fields mismatches / the attribute-free call errors.

- [ ] **Step 3: Update `real_gs_login`**

In `src/provgate/cli/wiring.py`, replace `real_gs_login` (lines 22-28):

```python
def real_gs_login(settings: Settings) -> GradescopeLogin:
    def login(email: str, password: str) -> GradescopeClient:
        client = GradescopeClient(
            httpx.Client(follow_redirects=True, timeout=settings.http_timeout_s),
            poll_interval_s=settings.gs_export_poll_interval_s,
            poll_timeout_s=settings.gs_export_poll_timeout_s,
        )
        client.login(email, password)
        return client

    return login
```

- [ ] **Step 4: Update the two call sites in `main.py`**

In `src/provgate/cli/main.py`, replace `real_gs_login(settings.http_timeout_s)` at line 134 with `real_gs_login(settings)`, and at line 179 replace `real_gs_login(settings.http_timeout_s)` with `real_gs_login(settings)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/ -v`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/provgate/cli/wiring.py src/provgate/cli/main.py tests/cli/test_wiring.py && uv run ruff format --check src/provgate/cli/wiring.py src/provgate/cli/main.py tests/cli/test_wiring.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/provgate/cli/wiring.py src/provgate/cli/main.py tests/cli/test_wiring.py
git commit -m "feat(cli): thread Gradescope export poll settings into the client"
```

---

### Task 7: Full-suite regression + docs

**Files:**
- Modify: `README.md` (only if it documents env vars — add the two poll vars)
- Test: whole suite

- [ ] **Step 1: Run the entire default suite**

Run: `uv run pytest`
Expected: PASS (all; `@pytest.mark.live` deselected). If anything fails, STOP and report — do not weaken tests.

- [ ] **Step 2: Full type check + lint**

Run: `uv run mypy --strict src && uv run ruff check . && uv run ruff format --check .`
Expected: no errors

- [ ] **Step 3: Document the new env vars (only if README lists env vars)**

Run `grep -n "PROVGATE_" README.md`. If an env-var/config section exists, add a concise note documenting `PROVGATE_GS_EXPORT_POLL_INTERVAL_S` (default 5) and `PROVGATE_GS_EXPORT_POLL_TIMEOUT_S` (default 600) — how long the gateway waits for Gradescope to generate a submission export. Match existing prose; do not add a new top-level section. If no env-var section exists, skip.

- [ ] **Step 4: Commit (only if README changed)**

```bash
git add README.md
git commit -m "docs: document Gradescope export poll env vars"
```

---

## Self-Review

**Spec coverage:**
- Real create→poll→S3 download flow, streaming to a temp file → Task 4 (client) + Task 5 (engine `with`). ✓
- Poll ready on `status=="completed"`, `progress` 0.0–1.0, `failed`/timeout errors → Task 4 (`_poll_generated_file`, `_PENDING_STATUSES`, timeout via injected clock) + tests. ✓
- CSRF sourced from the assignment page meta → Task 4 (`_csrf_token`) + test. ✓
- `prune_export` reads from a `Path` (large export off-heap), delta stays bytes → Task 2. ✓
- `parse_assignments` React `AssignmentsTable` props, `assignment_` prefix stripped, raises (never silent []) → Task 1; client wraps as `GradescopeError` → Task 4. ✓
- Config poll settings, env-overridable → Task 3; wired via `real_gs_login` → Task 6. ✓
- Port signature → `AbstractContextManager[Path]`; engine rewired; watermark/delta invariant unchanged → Task 5. ✓
- Secret + student-data hygiene (temp deleted on exit, no logging) → Task 4 client + constraints. ✓
- Not doing: co-location, grades, gradescopeapi → no tasks (correctly absent). ✓

**Placeholder scan:** No TBD/TODO/"add validation". All code shown in full. Task 6's test mocks the login round-trip and asserts the constructed client's poll settings (real behavioral wiring check). Task 7 Step 3 is conditional with an explicit skip. ✓

**Type consistency:** `download_export` returns a context manager yielding `Path` in the client (`@contextmanager` → `Iterator[Path]`), the port (`AbstractContextManager[Path]`), the engine (`with … as export_path`), and both fakes (`FakeGs` `@contextmanager` yielding `Path`) — consistent. `prune_export(Path | bytes, ...)` defined in Task 2 and called with a `Path` in Task 5. Poll kwargs `poll_interval_s`/`poll_timeout_s`/`sleep`/`monotonic` named identically in Task 4 (constructor), Task 4 tests (`make_client`), and Task 6 (wiring). `Settings.gs_export_poll_interval_s`/`gs_export_poll_timeout_s` consistent across Tasks 3 and 6. ✓

**Ordering:** Tasks must run 1→7. Task 4 depends on Task 1 (parse raises `ValueError`) and Task 3 (poll settings default in the constructor, so Task 4 is testable before Task 6 wires them). Task 5 depends on Tasks 2 and 4. Task 6 depends on Tasks 3 and 4.

**Note on `tests/cli/test_wiring.py`:** it may already exist from the chunked-upload plan (Task 5 there). If so, append the new test; if not, create it with the shown imports plus the new test.
