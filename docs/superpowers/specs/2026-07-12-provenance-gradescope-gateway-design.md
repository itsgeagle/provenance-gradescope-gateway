# Design Spec — Provenance ⇄ Gradescope Sync Gateway (`provgate`)

**Date:** 2026-07-12
**Status:** Approved design, pre-implementation
**Author:** brainstormed with Claude Code

## 1. Summary

`provgate` is a standalone Python service that, on a schedule, pulls newly-submitted
student work out of Gradescope and forwards it to a Provenance server's public
ingest API. It is a **multi-class sync manager**: you register N classes, each with
its own Gradescope login + course and a target Provenance semester, and one sync
pass services all of them.

It is a **pure HTTP client of Provenance** — no Provenance code, DB, or storage, and
**no changes to the Provenance monorepo**. The only hard surface is the Gradescope
side (an undocumented, changing endpoint set accessed with a staff credential),
which is fully quarantined in one module.

## 2. Goals / non-goals

**Goals**

- Register and manage multiple classes, each with independent Gradescope credentials,
  course, Provenance semester, Provenance token, and assignment scope.
- On each scheduled pass, forward only *new* submissions per assignment (incremental).
- Require zero changes to Provenance; use only its documented public API + a token.
- Keep credentials encrypted at rest and out of logs, argv, and git.
- Isolate failures: one class breaking never blocks the others.
- Be structured so a web GUI can be added later without touching sync logic.

**Non-goals**

- No modification of the Provenance monorepo.
- No reimplementation of Provenance analysis, dedup, roster, or bundle parsing.
- No scraping of grades, rubrics, or annotations (`export/without_evaluations` only).
- No plaintext secrets anywhere.
- No heavyweight scheduler/queue; the sync is a one-shot process driven by external cron
  (with a thin `--loop` fallback).

## 3. Context & data flow

1. A student records their work with the Provenance recorder → a sealed submission
   bundle ZIP (`manifest.json` + `manifest.sig` + `.slog`/`.slog.meta`).
2. The student uploads that ZIP to Gradescope, which **auto-extracts it on upload**, so
   Gradescope stores the loose bundle file tree (not the `.zip`).
3. `provgate` downloads the assignment's **native Gradescope bulk export**
   (`…/assignments/{aid}/export/without_evaluations`). This export is already in the
   exact shape Provenance's `ingest:gradescope` endpoint expects:
   `submission_metadata.yml` at the root plus one `submission_<id>/` folder per
   submission holding the auto-unzipped bundle contents.
4. `provgate` prunes the export to only new submissions and POSTs it to Provenance,
   which parses each folder, rebuilds a flat bundle ZIP, dedups by content hash,
   updates the roster, and runs analysis.

**Key realization from the Provenance codebase:** the server's Gradescope ingest path
(`packages/server/src/services/ingest/gradescope/parse-export.ts` →
`buildBundleZipForFolder`) already handles the "Gradescope auto-unzipped the bundle"
case — it rebuilds the flat bundle from a folder's loose files, finds the manifest,
handles macOS noise and group submissions, and rosters everyone from
`submission_metadata.yml`. So the gateway needs to reformat **nothing**; it only
*filters* the native export.

## 4. The Provenance contract (fixed interface)

- `POST /semesters/{semesterId}/ingest:gradescope` — multipart form, field `archive`
  = a Gradescope export ZIP. Requires a **write-scoped** Bearer API token. Returns
  `202 { job_id, roster, bundles_processed, submissions_queued, skipped }`.
  **No assignment id is sent**; Provenance derives assignment + roster from the export.
- `GET /semesters/{semesterId}/ingest/jobs/{jobId}` — poll until `status` ∈
  { `succeeded`, `partial`, `failed` }.
- **Dedup is Provenance's responsibility.** It dedups by `(semester_id, blob_sha256)`
  as pipeline phase 2, *before* the expensive heuristics/stats/validation, and the
  bundle rebuild is deterministic. Re-sending an unchanged bundle is cheap and safe.
  **Our watermark is therefore an optimization, not the correctness mechanism.**

## 5. Architecture

Layered core with dependencies pointing inward; frontends depend on core, core never
depends on a frontend.

```
frontends:   provgate/cli/         (later: provgate/web/)
                 │
core:        provgate/sync/        orchestration + pure delta/prune logic
             ├── provgate/gradescope/   authenticated client (undocumented API, quarantined)
             ├── provgate/provenance/   Provenance HTTP client (the 3 behaviors in §4)
             ├── provgate/store/        SQLite repo + secret encryption
             └── provgate/config.py     settings (store path, master key, timeouts, base URLs)
```

**Module responsibilities & interfaces**

- **`gradescope/`** — the *only* module that imports `gradescopeapi` or constructs a
  Gradescope URL. Exposes a typed interface: `login(email, password) -> Session`,
  `list_assignments(session, course_id) -> list[Assignment]`,
  `download_export(session, course_id, assignment_id) -> bytes`. All fragility
  (login/CSRF, HTML/endpoint drift, async-export poll/redirect) lives here. When
  Gradescope breaks, exactly one package changes.
  - **`gradescopeapi` gives us login/session + course/assignment listing but NOT
    submission/export download** — we implement `download_export` ourselves against
    `…/assignments/{aid}/export/without_evaluations`.
- **`provenance/`** — the only module that knows Provenance's HTTP shape. Exposes
  `ingest_gradescope_export(base_url, token, semester_id, zip_bytes) -> JobHandle` and
  `poll_job(base_url, token, semester_id, job_id) -> JobStatus`. Retries transient
  network errors; surfaces terminal job outcomes as typed values.
- **`store/`** — pure persistence; the only place SQL is written and the only
  encrypt/decrypt seam. Repository interface over the tables in §6.
- **`sync/`** — orchestration only, receiving the two clients + store via dependency
  injection so it is unit-testable against fakes. Contains the **pure** delta/prune
  functions (operate on in-memory bytes, no I/O).
- **`cli/`** — thin frontend: parse args, prompt for secrets, call core. No sync logic.
- **`config.py`** — settings from env: `PROVGATE_DB_PATH`, `PROVGATE_SECRET_KEY`,
  timeouts, default poll interval.

## 6. State store (SQLite, single file on a persistent volume)

- **`classes`** — `id`, `label` (unique), `gradescope_course_id`,
  `provenance_base_url`, `provenance_semester_id`, `assignment_policy`
  (`all` | `include:[ids]` | `exclude:[ids]`), `enabled`, timestamps.
- **`secrets`** — `class_id`, `kind` (`gradescope_password` | `provenance_token`),
  `ciphertext`. Encrypted at rest (Fernet; key from `PROVGATE_SECRET_KEY`). Gradescope
  email is stored in `classes` (not secret); the password/token are.
- **`forwarded_submissions`** — the watermark: `(class_id, gs_assignment_id,
  submission_key)` unique, plus `provenance_job_id`, `forwarded_at`.
- **`runs`** — audit log per pass: `class_id`, `gs_assignment_id`, started/finished,
  `delta_count`, `job_id`, `outcome` (`succeeded`/`partial`/`failed`/`skipped`/`error`),
  `error_summary` (redacted). Never contains secrets.

## 7. Sync flow (one pass)

For each **enabled** class (isolated in a try/except island; failure logged to `runs`,
never aborts other classes):

1. Decrypt the class's Gradescope credentials + Provenance token (in memory only).
2. `login` to Gradescope; reuse the session for the whole class within the pass.
3. Resolve in-scope assignments from `assignment_policy`.
4. For each in-scope assignment:
   a. `download_export` (bulk `export/without_evaluations`).
   b. **Validate** it looks like an export (contains `submission_metadata.yml`); error
      clearly otherwise.
   c. **Compute the delta** and **prune** (see §8). If empty → record `skipped`, continue.
   d. `ingest_gradescope_export` with the pruned ZIP → `job_id`.
   e. `poll_job` to a terminal state.
   f. On `succeeded`/`partial` → insert the forwarded submission keys into
      `forwarded_submissions`; record the `runs` row. On `failed`/error → record the
      failure, **leave the watermark untouched** (next run retries).

`--dry-run` performs steps a–c and reports the delta without POSTing.

## 8. Delta / pruning invariant (most important detail)

Incrementality is achieved by **pruning submission folders, never rewriting metadata.**

- Parse `submission_metadata.yml` **read-only** to enumerate submission folder keys.
- Build the pruned ZIP as: **the original `submission_metadata.yml` bytes, byte-for-byte
  unchanged**, plus only the submission folders whose key is not in the watermark, minus
  macOS noise (`.DS_Store`, `__MACOSX/`).
- Already-synced submissions therefore appear to Provenance as `skipped/no_manifest`
  (harmless — still rostered, no new bundle/ingest row). New submissions become bundles.
- **Never regenerate or hand-edit the metadata YAML** — round-tripping risks dropping
  fields Provenance reads. Copy verbatim; filter the file tree only.
- Advance the watermark only after a terminal `succeeded`/`partial`.

`prune_export(zip_bytes, already_forwarded: set[str]) -> PrunedExport` is a **pure
function** with no I/O and gets full-branch unit coverage. `PrunedExport` carries the
new ZIP bytes and the set of forwarded keys to commit on success.

**Re-submissions:** a student re-upload produces a new Gradescope submission key (the
export contains the active submission per student), so it is naturally not in the
watermark and gets forwarded. If watermark logic is ever wrong, Provenance's content-hash
dedup is the backstop — no duplicates result.

## 9. Management CLI (now) — `provgate`

- `keygen` — print a fresh Fernet master key.
- `class add|list|edit|remove` — manage class configs. **Secrets are prompted / read
  from stdin or env, never passed as flags.**
- `doctor --class <label>` — verify Gradescope login + course visibility + assignment
  resolution + Provenance token validity/scope, before trusting a class.
- `sync [--all | --class <label>] [--dry-run] [--loop --interval <dur>]`.
- `runs` — recent sync history.

A future `web/` GUI is just another caller of `store` + `sync`.

## 10. Secrets handling

- Gradescope passwords + Provenance tokens encrypted at rest (Fernet). Master key from
  `PROVGATE_SECRET_KEY` — lives only in the environment, never in the DB, image, or git.
- Plaintext secrets exist only transiently in memory during a sync.
- Redact `Authorization` / `Cookie` / `Set-Cookie` before any request/response logging.
- Secrets never appear in argv, logs, tracebacks, `runs` rows, or committed fixtures.

## 11. Deployment

- Docker image; `provgate sync --all` is one-shot (runs one pass, exits).
- Cadence driven by an external scheduler (apphost cron / systemd timer / GitHub
  Actions), with a thin `--loop --interval 1h` fallback for hosts without cron.
- Persistent volume for the SQLite store (`PROVGATE_DB_PATH`) so watermarks survive
  restarts.
- Network egress required to `gradescope.com` and each Provenance server.

## 12. Dependencies (new project; flagged for approval)

`gradescopeapi` (login/session + assignment listing), `httpx` (export download +
Provenance client), `cryptography` (Fernet secret encryption), `PyYAML` (read-only
metadata parse), `typer` (CLI), stdlib `sqlite3`. Dev: `pytest`, `respx`, `ruff`,
`mypy`. Managed with `uv`.

## 13. Testing strategy

- **Deterministic, no live network by default.** Real-Gradescope/real-Provenance tests
  behind `@pytest.mark.live` (opt-in, require env creds), excluded from CI default.
- Unit: `prune_export` (full-branch) against fixture exports; assignment-policy
  resolution; store CRUD; secret encryption round-trip (ciphertext ≠ plaintext).
- Client tests mock HTTP via `respx`/recorded fixtures, including the Gradescope
  async-export poll/redirect path and an export missing `submission_metadata.yml`.
- One end-to-end: fixture Gradescope export → fake Provenance server → assert (a)
  delta-only forwarding on a second run, (b) a `failed` job leaves the watermark unmoved,
  (c) one class's failure doesn't abort another class in the same pass.

## 14. Open risks (isolated, mitigated)

- **Undocumented Gradescope surface** (login + export URL may change) — quarantined in
  `gradescope/`, pinned with fixtures, `doctor` catches breakage early.
- **Large / async exports** — export endpoint may return a "preparing" redirect; the
  client handles poll/redirect rather than assuming a synchronous body.
- **Login lockout / rate limits** — reuse one session per class per pass; back off on
  429/auth errors.
- **Secret-at-rest** — encrypted store + strict file perms; master key from env; losing
  the key means re-entering credentials (documented).

## 15. Future / parked

- Web GUI (core is already frontend-agnostic).
- Upstream PR to `gradescopeapi` adding a submission-export download path, once the
  `download_export` implementation here is proven.
