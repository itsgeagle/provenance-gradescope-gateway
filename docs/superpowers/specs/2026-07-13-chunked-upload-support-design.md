# Chunked upload support in `provgate/provenance/`

**Date:** 2026-07-13
**Status:** Approved design, ready for implementation planning
**Scope:** `src/provgate/provenance/client.py` + two new `src/provgate/config.py` settings. No other module changes.

## Problem

`provgate` forwards each pruned Gradescope export to Provenance with a single
multipart POST to `…/ingest:gradescope` (`provenance/client.py`). The Provenance
`apphost` deploy sits behind a reverse proxy (nginx) whose `client_max_body_size`
is ~20 MiB. Any single request body above that cap is rejected with `413` **before
it reaches the app** — regardless of the app's own 10 GiB streaming ceiling.

Today `provgate` has no size handling: a pruned delta larger than the proxy cap
POSTs its whole body and gets a `413`. The client raises `ProvenanceError`, the
class's sync is isolated, and the watermark is (correctly) left unadvanced — so it
retries the same oversized POST every pass and `413`s forever. That assignment is
stuck.

Pruning masks this most of the time (a typical hourly delta is a handful of small
recorder bundles), so it only bites when one delta exceeds the cap — a burst of
simultaneous submissions, or a large bundle.

## What Provenance already offers

The Provenance server exposes a resumable, S3-multipart-backed upload flow
alongside the single-POST path (`packages/server/src/api/v1/routes/ingest.ts`).
The official analyzer frontend uses it for exports at/above 16 MiB
(`packages/analyzer/src/api/resumable-upload.ts`), splitting them into ≤16 MiB
parts so each request clears the ~20 MiB proxy cap.

`provgate` will adopt the same flow. This is a deliberate expansion of the
Provenance contract that `CLAUDE.md` pins (the three `ingest:gradescope` + poll
behaviors) to include the resumable upload endpoints. The expansion was reviewed
and approved before this spec was written.

## The fixed server contract (transport being adopted)

Below the threshold — unchanged from today:

- `POST /semesters/{semesterId}/ingest:gradescope` — multipart, field `archive`,
  → `202 { job_id, … }`.

At/above the threshold — the resumable flow:

- `POST /semesters/{semesterId}/ingest/uploads`
  body `{ filename, total_bytes, chunk_size }`
  → `201 { upload_id, s3_upload_id, chunk_size, total_parts }`.
  The server clamps the requested `chunk_size` to `[5 MiB, 512 MiB]` and returns
  the effective value plus the derived `total_parts`. The client MUST use the
  **returned** `chunk_size` and `total_parts`, not its requested values.
- `PUT /semesters/{semesterId}/ingest/uploads/{uploadId}/parts/{partNumber}?s3_upload_id=…`
  body = raw chunk bytes → `200 { part_number, received }`. Parts are 1-indexed.
- `POST /semesters/{semesterId}/ingest/uploads/{uploadId}/complete`
  body `{ s3_upload_id }` → `202 { job_id, …placeholder roster/counts }`.
- `DELETE /semesters/{semesterId}/ingest/uploads/{uploadId}?s3_upload_id=…`
  → `204`. Aborts the S3 multipart upload.

All requests carry the write-scoped Bearer token. After `/complete`, the existing
`poll_job(job_id)` drives the job to a terminal state, unchanged.

**Response-body note:** the chunked `/complete` returns placeholder
`roster`/`bundles_processed`/`submissions_queued` in its 202; the real numbers
arrive only via job polling. This does **not** affect `provgate`: the engine reads
only `handle.job_id` and the polled terminal `status`, and computes `delta_count`
/ `total_submissions` locally from the pruned export. Notify/render read those same
engine-local fields. Verified in `sync/engine.py` and `sync/ports.py`.

## Design

### Seam (what does and does not change)

The change is quarantined behind the existing port method:

```
ProvenancePort.ingest_gradescope_export(base_url, token, semester_id, zip_bytes) -> JobHandle
```

Its signature and return type are unchanged. Internally it branches on
`len(zip_bytes)`. Because the seam is stable:

- `sync/engine.py` — unchanged.
- `sync/ports.py` (the Protocol) — unchanged.
- `store/` — unchanged (no cross-pass resume state; see below).
- `notify/` — unchanged.
- `cli/` — unchanged except wiring the two new settings (already flows through
  `Settings`; no new call sites if the settings have defaults).

### Client changes (`provenance/client.py`)

New private helpers on `ProvenanceClient`:

- `_create_upload(base_url, token, semester_id, filename, total_bytes, chunk_size) -> (upload_id, s3_upload_id, chunk_size, total_parts)`
- `_put_part(base_url, token, semester_id, upload_id, s3_upload_id, part_number, body)` — with retry.
- `_complete_upload(base_url, token, semester_id, upload_id, s3_upload_id) -> JobHandle`
- `_abort_upload(base_url, token, semester_id, upload_id, s3_upload_id)` — best-effort.

`ingest_gradescope_export` branches:

- `len(zip_bytes) < ingest_chunk_threshold_bytes` → existing single POST
  (byte-for-byte the current behavior).
- else → `_create_upload` → slice `zip_bytes` into `total_parts` parts of the
  server-returned `chunk_size` (final part is the short remainder) → `_put_part`
  for each 1-indexed part → `_complete_upload` → return `JobHandle(job_id)`.

Part slicing uses the server-returned `chunk_size` and `total_parts`. Slicing is a
pure in-memory operation on the already-in-memory `zip_bytes`; no new streaming or
disk I/O (the pruned ZIP is already fully in memory upstream).

### Retry (per-part only)

Each `_put_part` is retried up to 4 attempts with exponential backoff
(`500ms · 2^attempt`), mirroring the analyzer's `MAX_PART_ATTEMPTS`. Backoff sleep
uses the injected `sleep` callable already on `ProvenanceClient`, so tests stay
deterministic. No cross-pass persistence, no store table, no `localStorage`
equivalent — a `provgate` pass is one-shot.

### Failure handling

On any unrecoverable failure during the chunked flow — a part exhausting its
retries, a non-`201/200/202` status, or `/complete` failing — the client makes a
best-effort `_abort_upload` (`DELETE`) so no orphaned S3 multipart parts linger
server-side, then raises `ProvenanceError`. The abort itself swallows errors (it is
cleanup, not correctness). The engine's per-assignment `try/except` isolates the
failure, records a `runs` row, and leaves the watermark unadvanced. The next
scheduled pass re-downloads, re-prunes, and retries; Provenance dedups by
`(semester_id, blob_sha256)` so any re-sent bundle is cheap and safe. This is the
same failure-safe story as the single-POST path today.

### Configuration (`config.py` + env)

Two new `Settings` fields, both with defaults, both tunable per-deploy via env:

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `ingest_chunk_threshold_bytes` | `PROVGATE_INGEST_CHUNK_THRESHOLD_BYTES` | `16 * 1024 * 1024` (16 MiB) | Pruned ZIPs `>=` this use the chunked path. |
| `ingest_chunk_size_bytes` | `PROVGATE_INGEST_CHUNK_SIZE_BYTES` | `16 * 1024 * 1024` (16 MiB) | Requested `chunk_size` hint on create. |

Defaults match the analyzer's values (safe under the assumed ~20 MiB nginx cap). A
deploy whose proxy cap differs adjusts via env, no code change. The threshold keeps
every request body — on both paths — under the cap: a sub-threshold single POST is
itself `< 16 MiB`, and an at/above export is split into `<= chunk_size` parts.

### Correctness invariants (unchanged)

- The watermark advances **only** after `poll_job` returns `succeeded`/`partial`.
  Chunking is pure transport and does not touch delta/prune/watermark logic.
- The pruned `submission_metadata.yml` and folder bytes are unchanged; chunking
  splits the already-built ZIP at arbitrary byte boundaries and the server
  reassembles it before any parsing.
- Secrets: the Bearer token is attached to every new request via the existing
  `_auth` helper. No token, `s3_upload_id`, or part body is logged. `s3_upload_id`
  is a capability secret and must be redacted if any request is logged.

## Testing

Unit tests in `tests/provenance/` using `respx` to mock the four endpoints:

1. **Sub-threshold** → single-POST path is used; none of the `/ingest/uploads*`
   endpoints are called.
2. **At/above threshold** → `create` → `parts` → `complete` sequence fires with the
   correct number of parts and byte-exact slicing, including a short final part.
   Assert the reassembled parts equal the original `zip_bytes`.
3. **Server clamps chunk_size** → client uses the returned `chunk_size`/`total_parts`,
   not its requested values.
4. **Transient part failure** → a part that returns `500` twice then `200` is
   retried and the upload completes; assert backoff sleeps via the injected clock.
5. **Unrecoverable part failure** → a part that exhausts retries triggers a
   `DELETE` abort and raises `ProvenanceError`.
6. **job_id flow** → the `job_id` from `/complete` is the one passed to `poll_job`.

Existing engine and end-to-end tests remain green untouched, demonstrating the
branch is transparent above the client seam. `mypy --strict` clean with no new
`Any`; `ruff` clean.

## Explicitly not doing (YAGNI)

- Cross-pass resume / a store handle table / `GET …/parts` reconciliation.
- Progress reporting.
- The generic multi-file `POST …/ingest` route — only `ingest:gradescope` is in
  scope.
- Streaming the pruned ZIP from disk — it is already fully in memory upstream.
- **Gradescope-side large/async download handling** — the async "preparing"
  redirect/poll and the whole-export-in-memory + pure-`prune_export` model are a
  distinct concern in `gradescope/` (not `provenance/`) with no shared code. Tracked
  as a separate follow-up spec. The Provenance proxy cap addressed here is on the
  upload path only; Gradescope downloads do not traverse it.
