# Gradescope submission-export download + assignment listing (real flows)

**Date:** 2026-07-13
**Status:** Approved design, ready for implementation planning
**Scope:** `src/provgate/gradescope/` (client + `parse.py`), `src/provgate/sync/prune.py`, `src/provgate/sync/engine.py` + `ports.py`, two new `config.py` settings. No Provenance changes.

Both the download **and** the assignment-listing paths in the current client are
broken against live Gradescope for the same root cause — Gradescope now renders
these instructor pages client-side (React), so the server HTML the current code
scrapes doesn't contain what it expects. Both were verified live during design.

## Problem

`GradescopeClient.download_export` does a single `GET
…/assignments/{aid}/export/without_evaluations` and returns `resp.content`
(`gradescope/client.py`). Against live Gradescope this **404s** — that route
does not exist. The endpoint, the sync-vs-async question, and the top-level ZIP
name in the design docs were all unverified assumptions (the client header and
the original plan flagged a live spike as required; it had never been run).

A `@pytest.mark.live` spike (`tests/gradescope/test_export_live.py`) captured the
real behavior. This spec is written against that observed flow, not assumptions.

Secondary: the current pipeline is fully in-memory (`download_export -> bytes`,
`prune_export(bytes)`, upload). A large export would spike RAM / hit the
multipart ~2 GiB in-memory ceiling. We fix that here by streaming the large part
(the full export) through disk while keeping only the small delta in memory.

Also broken: **`list_assignments`** (`parse_assignments`) scrapes
`<a href="/courses/.../assignments/{id}">` anchors from the course page, but the
instructor assignments page is a **React table** (`data-react-class="AssignmentsTable"`)
with no such anchors — it returns an empty list live (verified: 0 assignments for
a course that has 15). Since the engine resolves which assignments to sync from
`list_assignments`, this breaks the sync end-to-end regardless of the download fix,
so it is fixed here too.

## Observed contract (from the live spike)

Gradescope's bulk **submission** export is an async, three-step flow. (Grades are
a *separate* "Download Grades" button/endpoint that provgate never calls — so the
submission export is inherently grade-free; there is no "with/without evaluations"
variant to select. The old `/export/without_evaluations` path is gone.)

1. **Create** — `POST /courses/{cid}/assignments/{aid}/export`
   Headers: session cookies (from login), `X-CSRF-Token` (the `csrf-token` meta
   value on any staff assignment page), `X-Requested-With: XMLHttpRequest`.
   Empty body. → `200 {"generated_file_id": <int>}`.
2. **Poll** — `GET /courses/{cid}/generated_files/{generated_file_id}.json`
   → `{ "id", "progress", "status", "url", "expires_at" }`.
   - `progress` is a **0.0–1.0 fraction** (NOT 0–100).
   - Ready when **`status == "completed"`**. `status` was `"processing"` while
     generating (observed ~65 s / ~14 polls for a 4.6 MB export). Treat any
     terminal non-`completed` status (e.g. `"failed"`) as an error.
   - `url` is present from `progress=0`, pointing at a not-yet-existing S3 object
     — it is only usable once `status=="completed"`.
3. **Download** — GET the ready `url`: a **presigned S3 URL**
   (`…s3-us-west-2.amazonaws.com/uploads/generated_file/file/{id}/submissions.zip?X-Amz-…`,
   `X-Amz-Expires=10800` = 3 h). Response: `200`, `Content-Type: application/zip`,
   `Content-Length` present, streamable.

**ZIP shape:** single top-level folder `assignment_{aid}_export`,
`submission_metadata.yml` present at its root, one `submission_{submission_id}/`
folder per submission. The top-level name is dynamic — never assume it (the
existing `prune._locate_metadata` already derives the prefix from the metadata
location, so this is handled).

If any of these behaviors appears to have changed at implementation time, re-run
the spike and update this spec — do not guess.

## Design

### Gradescope client (`gradescope/client.py`)

Replace `download_export` with the real flow. It stays the only module that knows
Gradescope's shape.

New/changed methods on `GradescopeClient`:

- `download_export(self, course_id, assignment_id) -> AbstractContextManager[Path]`
  — a context manager that runs create → poll → download, streams the S3 body to
  a **temp file**, yields its `Path`, and deletes it on exit (even on error). The
  full export never enters RAM.
- Internals (private): `_csrf_token(course_id, assignment_id) -> str` (GET a staff
  page — the assignment page, which redirects to `review_grades` — and extract the
  `csrf-token` meta), `_create_export(...) -> int` (POST, returns
  `generated_file_id`), `_poll_generated_file(course_id, gfid) -> str` (poll the
  `generated_files/{id}.json` route until `status=="completed"`, returns the S3
  `url`; raises on `failed`/timeout), `_download_to_temp(url) -> Path` (stream to
  temp file).

Poll cadence uses an injected `sleep`/`monotonic` (like `ProvenanceClient`) so
tests are deterministic. Interval/timeout come from config (below).

`GradescopeError` is raised on: missing csrf meta, non-200 create, `failed`/other
terminal status, poll timeout, non-200 download. Each is isolated per-assignment
by the engine.

### Assignment listing (`gradescope/parse.py`, `gradescope/client.py`)

`list_assignments` keeps its signature (`course_id -> list[Assignment]`) and its
`GET /courses/{cid}/assignments`. Only `parse_assignments` changes: instead of
anchor scraping, extract the React component's props.

Observed shape: the page contains
`<div data-react-class="AssignmentsTable" data-react-props="{HTML-escaped JSON}">`.
The (unescaped) JSON has `table_data`: a list of rows, each with `id`
(`"assignment_8255863"` — strip the `assignment_` prefix for the numeric id),
`title` (`"Lab 0"`), and `url` (`/courses/{cid}/assignments/{id}`).

`parse_assignments` becomes: locate the `AssignmentsTable` `data-react-props`,
`html.unescape` + `json.loads` it, and map each `table_data` row to
`Assignment(id=<row.id without the "assignment_" prefix>, title=<row.title>)`.
Validate defensively (missing component / bad JSON / absent `table_data` → a clear
`GradescopeError`, not a silent empty list — an empty list would look like "no
assignments" and silently sync nothing). `Assignment` (`id`, `title`) is unchanged.

### Streaming / memory model

The full export is large; the pruned delta is small (only new submissions). So:

- **Download → temp file** (client) — the big bytes live on disk, not RAM.
- **Prune reads the temp file** — `zipfile.ZipFile(path)` reads the central
  directory and one entry at a time from disk; it does **not** load the whole
  archive into memory. The pruned **delta** is written to an in-memory buffer
  (small) and returned as `zip_bytes`, exactly as today.
- **Upload the delta** via the existing chunked path (unchanged;
  `ingest_gradescope_export(..., zip_bytes)`), which already handles the proxy
  request-size cap.

Only the small delta is ever fully in memory. Temp files are deleted after the
pass — consistent with "student source is not retained beyond the run".

### Prune change (`sync/prune.py`)

Minimal: `prune_export` accepts a **file source** in addition to bytes. Change the
signature to `prune_export(source: Path | bytes, already_forwarded: set[str]) ->
PrunedExport`. When `source` is bytes, wrap in `io.BytesIO` (back-compat for
existing tests); when it is a `Path`/str, pass it straight to `zipfile.ZipFile`,
which streams from disk. All existing pure logic (`_locate_metadata`, `_key_for`,
`_is_noise`, verbatim metadata copy, delta selection) is unchanged — so the
load-bearing correctness (which keys to keep, byte-for-byte metadata) keeps its
exhaustive tests. The delta invariant (§ CLAUDE.md) is untouched: metadata copied
verbatim, only new `submission_*` folders included, watermark advanced only on a
terminal `succeeded`/`partial` Provenance job.

`PrunedExport` (`zip_bytes`, `forwarded_keys`, `total_submissions`) is unchanged.

### Engine rewiring (`sync/engine.py`, `sync/ports.py`)

`GradescopePort.download_export` becomes
`download_export(course_id, assignment_id) -> AbstractContextManager[Path]`.
`_sync_assignment` changes from:

```python
export = gs.download_export(cfg.gradescope_course_id, aid)   # bytes
pruned = prune_export(export, already)
```

to:

```python
with gs.download_export(cfg.gradescope_course_id, aid) as export_path:
    pruned = prune_export(export_path, already)   # reads the temp file
# temp file deleted here; pruned.zip_bytes (the small delta) remains
```

Everything downstream (delta count, dry-run, ingest, poll, watermark, run
recording) is unchanged. Per-assignment try/except isolation is unchanged, so a
download/poll failure for one assignment is recorded and does not abort the pass.

### Config (`config.py` + env)

Two new `Settings` fields (with defaults, env-overridable):

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `gs_export_poll_interval_s` | `PROVGATE_GS_EXPORT_POLL_INTERVAL_S` | `5.0` | Delay between generated-file status polls. |
| `gs_export_poll_timeout_s` | `PROVGATE_GS_EXPORT_POLL_TIMEOUT_S` | `600.0` | Max wait for `status=="completed"`. |

Wired into `GradescopeClient` construction in `cli/wiring.py` (the login factory
already builds the client there).

### Invariants & hygiene

- Watermark advances only after the Provenance job is `succeeded`/`partial`
  (unchanged). A failed/timed-out export leaves the watermark untouched → retried
  next pass; Provenance dedup covers any re-send.
- Secrets: never log the password, session cookies, the `X-CSRF-Token`, or the
  **presigned S3 URL** (its `X-Amz-Signature` query string is a bearer capability
  — redact it if any download URL is ever logged). No `Authorization`/`Cookie`
  header values logged.
- Student source: the export temp file is deleted after each assignment; nothing
  is retained. The pruned delta is streamed to Provenance and dropped.

## Testing

Default suite (`respx`, no network):

- **Client flow:** mock create (`200 {generated_file_id}`) → poll
  (`processing`×N then `completed` with an S3 `url`) → S3 download (`200`
  `application/zip` body). Assert the temp file is produced, then deleted on
  context exit, and the bytes match.
- **CSRF sourcing:** the page GET's `csrf-token` meta is sent as `X-CSRF-Token` on
  the create POST.
- **Not-ready guard:** a poll returning `url` while `status=="processing"` does
  NOT trigger download; only `completed` does.
- **Failure paths:** `status=="failed"` → `GradescopeError`; poll never completing
  within timeout → `GradescopeError` (deterministic via injected clock);
  non-200 create/download → `GradescopeError`.
- **Assignment listing:** `parse_assignments` against a captured `AssignmentsTable`
  React-props fixture returns the right `(id, title)` pairs with the `assignment_`
  prefix stripped; a page missing the component or with malformed props raises
  `GradescopeError` (not an empty list). Update the existing anchor-based
  `parse_assignments` fixtures/tests to the React-props shape.
- **Prune path-source:** existing fixture-based prune tests extended to pass a
  temp-file `Path` (not just bytes) and assert identical `PrunedExport`. Existing
  byte-based tests stay green (back-compat).
- **Engine e2e:** a fake `GradescopePort.download_export` yielding a fixture export
  path → fake Provenance → assert (a) only the delta is forwarded on a second run
  and (b) a `failed` job leaves the watermark unmoved (the existing e2e, adapted
  to the context-manager interface).

The live spike (`tests/gradescope/test_export_live.py`, `@pytest.mark.live`)
remains the ground-truth capture and the way to re-verify if Gradescope changes.

`mypy --strict src` clean (no new `Any` beyond the existing scraping boundary);
`ruff` clean.

## Explicitly not doing (YAGNI / out of scope)

- **Co-location / local-path ingest.** Provenance's `ingestLocalPath` is not an
  HTTP endpoint (only an internal service + a server-side CLI), so a shared-volume
  handoff would require a Provenance server change — which this repo does not
  make. provgate stays a pure HTTP client and must not depend on co-location. A
  future `ingest:localpath` proposal to Provenance is a separate conversation.
- **Grades / evaluations export** — a separate Gradescope button/endpoint we never
  call.
- **Streaming the delta upload from disk** — the delta is small; it stays in
  memory and uses the existing chunked uploader. Revisit only if deltas grow.
- **`gradescopeapi` adoption** — the client stays hand-rolled on `httpx`; we do not
  take the library as a dependency (it does not cover the bulk-export download, and
  it would split the authenticated session the create/poll/download flow needs).
