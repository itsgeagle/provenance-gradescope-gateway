# CLAUDE.md

Project conventions and standing instructions for Claude Code working in this repo. Read this fully before doing anything.

## What this is

**provenance-gradescope-gateway** (`provgate`): a standalone Python service that syncs newly-submitted student work from Gradescope into a [Provenance](https://github.com/ProvenanceTools/provenance) server on a schedule. It is a **multi-class sync manager** — you register N classes, each with its own Gradescope login + course and a target Provenance semester, and one sync pass services all of them.

It is a **pure HTTP client of Provenance's public API**. It holds no Provenance code, database, or storage; it authenticates with a Provenance API token exactly like any third-party tool. The only "hard" surface is the *Gradescope* side: an undocumented, changing endpoint set accessed with a staff credential.

The data flow it automates:

1. A student records their work with the Provenance recorder → a sealed submission bundle ZIP (`manifest.json` + `manifest.sig` + `.slog`/`.slog.meta`).
2. The student uploads that ZIP to Gradescope, which **auto-extracts it on upload** (so Gradescope stores the loose bundle file tree, not the `.zip`).
3. `provgate`, hourly, pulls each configured assignment's **native Gradescope bulk export** (`…/assignments/{aid}/export/without_evaluations`) — already in the exact shape Provenance's `ingest:gradescope` endpoint expects.
4. It forwards only the *new* submissions (delta) to `POST /semesters/{id}/ingest:gradescope`, polls the returned job to a terminal state, and advances a per-assignment watermark.

**This repo never changes the Provenance monorepo.** Everything here talks to Provenance over its documented HTTP API only.

## The Provenance contract (do not reimplement Provenance)

The gateway depends on exactly three public Provenance behaviors. Treat them as the fixed interface:

- `POST /semesters/{semesterId}/ingest:gradescope` — multipart form, field name `archive`, value a Gradescope "Download Submissions" export ZIP. Returns `202 { job_id, roster, bundles_processed, submissions_queued, skipped }`. Requires a **write-scoped** API token (Bearer). **No assignment id is sent** — Provenance derives assignment identity and roster from the export's `submission_metadata.yml` + each bundle's signed manifest.
- `GET /semesters/{semesterId}/ingest/jobs/{jobId}` — poll until `status` is terminal (`succeeded` / `partial` / `failed`).
- Content-hash dedup is Provenance's job, not ours. Provenance dedups by `(semester_id, blob_sha256)` **before** any heavy processing. Re-sending an unchanged bundle is cheap and safe on Provenance's side. **Our watermark is an optimization, never the correctness mechanism** — if the watermark is wrong, Provenance's dedup still prevents duplicate submissions.

If any of these behaviors appears to have changed, **stop and ask** — do not work around it. The contract is owned by the Provenance server, not by this repo.

## Working agreement

- **Stop and ask on ambiguity.** If a decision isn't covered by the spec or this file, do not invent an answer. Ask. Inventing behavior around an undocumented external API is the single biggest failure mode on this project.
- **Stay in scope.** Touch only the files the current task requires. Do not opportunistically refactor. Do not "improve" things that weren't asked about. If you notice something that should change, mention it in your response; do not change it.
- **No new dependencies without asking.** Every dependency added to a service that holds credentials and scrapes an undocumented API is a decision. Propose, justify, wait for approval. The approved set is small (see Architecture rules).
- **No silent constraint softening.** If a test is failing and the obvious fix is to weaken the assertion, stop and explain. Tests encode requirements; loosening them is a product decision, not a coding decision.
- **Read before writing.** Before editing any file, read it. Before editing any module, read its tests.
- **Small diffs.** If a change touches more than ~200 lines across more than ~5 files, it's probably two changes. Split it.
- **Secrets never enter argv, logs, tracebacks, or test fixtures.** See "Things that are easy to get wrong here."

## Architecture rules

The core is layered so a future web GUI can be added as just another frontend without touching sync logic. Dependencies point **inward**; frontends depend on core, core never depends on a frontend.

```
frontends:   provgate/cli/        (later: provgate/web/)
                 │  depends on ↓
core:        provgate/sync/       orchestration: per class → per assignment → delta → POST → poll
             ├── provgate/gradescope/   authenticated client: login, list assignments, download export
             ├── provgate/provenance/   API client: ingest:gradescope, poll job
             ├── provgate/store/        SQLite repository: classes, secrets, watermarks, run log
             ├── provgate/notify/       pure render_summary + best-effort post_summary (webhook)
             └── provgate/config.py     settings: store path, master key, timeouts, base URLs
```

- **`provgate/gradescope/` is the only module that knows Gradescope exists.** All undocumented-API fragility is quarantined here: login, session/CSRF handling, assignment listing, and export download. It exposes a small typed interface (`list_assignments`, `download_export`) to the rest of the app. The client is hand-rolled on `httpx` (we do **not** depend on `gradescopeapi`); nothing outside this package constructs a Gradescope URL or touches its session/HTML. When Gradescope breaks, exactly one package changes.
- **`provgate/provenance/` is the only module that knows Provenance's HTTP shape.** It exposes `ingest_gradescope_export(...) -> JobHandle` and `poll_job(...) -> JobStatus`. It never reaches into Provenance internals; it only calls the three public behaviors above.
- **`provgate/store/` is pure persistence.** It owns the SQLite connection and is the only place SQL is written. It exposes a repository interface (classes, secrets, watermarks, runs). No business logic, no HTTP, no Gradescope/Provenance types leaking in.
- **`provgate/sync/` is orchestration only.** It receives the two clients and the store via constructor/params (dependency injection) so it can be unit-tested against fakes. It contains the delta computation and ZIP pruning — the heart of the app — as **pure functions** operating on in-memory bytes, separate from any I/O.
- **`provgate/cli/` is a thin frontend.** It parses args, prompts for secrets, and calls core. No sync logic lives here. A `web/` frontend added later must reuse `store` + `sync` unchanged.
- Secrets (Gradescope passwords, Provenance tokens) are **encrypted at rest** in the store (Fernet; master key from `PROVGATE_SECRET_KEY`). Plaintext secrets exist only transiently in memory during a sync. There is exactly one encrypt/decrypt seam and it lives in `store`.
- `provgate/notify/` is optional and isolated: `render.py` is a pure function of a pass's results, `webhook.py` POSTs it and swallows every exception. A dead or misconfigured webhook must never affect sync correctness — the CLI only fires the post after the pass has already completed and been reported.

## The delta / pruning invariant (the one thing that matters most)

Incrementality is achieved by **pruning submission folders, never by rewriting Gradescope's metadata.**

- Download the full bulk export. Parse `submission_metadata.yml` read-only to enumerate submission folder keys.
- Build the pruned ZIP as: **the original `submission_metadata.yml` bytes, byte-for-byte unchanged**, plus only the submission folders whose key is not already in the watermark, minus macOS noise (`.DS_Store`, `__MACOSX/`).
- Already-synced submissions therefore appear to Provenance as `skipped/no_manifest` (harmless: they are still rostered, but produce no new bundle or ingest row). New submissions become bundles.
- **Never regenerate, re-serialize, or hand-edit `submission_metadata.yml`.** Round-tripping YAML risks dropping fields Provenance reads. The metadata is copied verbatim; only the file tree is filtered.
- Advance the watermark **only after** the Provenance job reaches `succeeded` or `partial`. On `failed` or any error, leave the watermark untouched so the next run retries.

Pruning logic is a pure function `prune_export(zip_bytes, already_forwarded: set[str]) -> PrunedExport`. It gets exhaustive unit tests against fixture exports. It never does I/O.

## Code style

- Python 3.11+. `mypy --strict` clean — no `Any` except at the HTML-scraping boundary, with a comment explaining why.
- `ruff` for lint **and** format (no separate Black). CI fails on lint or format drift.
- Prefer pure functions over classes when there's no state to own. The delta/prune logic is pure functions. `store` is a class because it owns a connection; the Gradescope client is a class because it owns a session.
- Errors are values when expected — return a result/`enum` outcome or a typed exception hierarchy the caller handles. Never swallow an exception silently. One class's sync failure is logged and isolated; it never aborts the pass for other classes.
- Type every public function signature. Use `dataclass`/`TypedDict`/`pydantic` models at boundaries; validate untrusted input (the Gradescope export, API responses) rather than trusting shape.
- No network calls, no `sqlite3`, no filesystem access inside pure logic modules — inject those via the client/store interfaces so tests need no network or disk.
- Structured logging only. Log class label, assignment id, counts, job ids, and outcomes. **Never log a password, token, cookie, or `Authorization`/`Set-Cookie` header value.**

## Testing

- `pytest` across the package. Co-located under `tests/` mirroring the module tree.
- **Deterministic.** No wall-clock or randomness in assertions — inject a clock. Freeze timestamps in fixtures.
- **No live network in the default suite.** Mock HTTP with `respx`/recorded fixtures. Real-Gradescope and real-Provenance tests live behind an explicit opt-in marker (`@pytest.mark.live`) and require credentials in the environment; they never run in CI by default.
- Every behavior change ships with tests. Bug fixes get a regression test that fails before the fix.
- The delta/prune function gets full-branch coverage — it's small and load-bearing (mirrors how Provenance treats its hash chain).
- One end-to-end test: a fixture Gradescope export → a fake Provenance server → assert (a) only the delta is forwarded on a second run, and (b) a `failed` job leaves the watermark unmoved.
- Secret encryption gets a round-trip test; assert ciphertext ≠ plaintext and that the store never returns plaintext from a read that shouldn't decrypt.

## Things that are easy to get wrong here

- **Metadata rewriting.** Don't. Copy `submission_metadata.yml` verbatim; filter folders only. See the delta invariant above.
- **Watermark vs. correctness.** The watermark is an optimization; Provenance dedup is correctness. Never let watermark logic gate whether a *changed* submission is sent — a re-submission gets a new Gradescope submission key and must be forwarded. When unsure, forward; Provenance dedups.
- **Advancing the watermark on failure.** Only advance after a terminal `succeeded`/`partial` job. A crash mid-poll must not lose submissions.
- **Secrets leaking.** Passwords/tokens must never appear in argv (CLI reads them from stdin/prompt/env, not flags), logs, exception messages, `runs` audit rows, or committed fixtures. Redact `Authorization`/`Cookie` headers before logging any request.
- **Gradescope export shape drift.** The export is undocumented. Validate that a downloaded export actually contains `submission_metadata.yml` before treating it as one; surface a clear error otherwise. Never assume the top-level folder name.
- **Large / async exports.** For big assignments Gradescope may return a "preparing" redirect rather than the ZIP immediately. The export download must handle poll/redirect, not assume a synchronous body.
- **Login lockout / rate limits.** Per-class credentials mean many logins per pass. Reuse a session per class within a pass; back off on 429/auth errors; never hammer.
- **Cross-class isolation.** One class's Gradescope outage or bad credential must not abort the sync for other classes. Each class is a try/except island with its own `runs` row.
- **Idempotent runs.** Re-running `sync` after a partial failure must be safe and must not double-forward already-succeeded submissions.

## Things we are explicitly not doing

- Modifying the Provenance monorepo. This tool is an API client; if it seems to need a server change, stop and ask.
- Reimplementing Provenance analysis, dedup, roster, or bundle parsing. Provenance owns all of that.
- Downloading or storing student source for any purpose other than immediate forwarding. The pruned export is streamed to Provenance and not retained beyond the run.
- Scraping grades, rubrics, or annotations. We fetch `export/without_evaluations` (raw submissions) only.
- Persisting secrets in plaintext, in a config file, or in git.
- A heavyweight scheduler or job queue. The sync is a one-shot `provgate sync --all` invoked by an external scheduler; a `--loop` mode is a thin convenience, not an orchestration framework.

## Conventions for talking to me

- When you finish a task, summarize what you did, what you didn't do, and what you noticed but didn't change.
- If you make a non-obvious choice, explain it in the response. Don't bury it in a comment.
- If you used a dependency you weren't told to use, surface it. If you skipped a test you couldn't get to pass, surface it. Anything I'd want to know on review, lead with it.
- "Done" means: tests pass, `mypy --strict` clean, `ruff` clean, diff is reviewable. Not "I wrote some code."

## Commands

(Tooling target — `uv` for env/deps. Finalized when the project is scaffolded; do not add commands to config without asking.)

- `uv sync` — install/lock dependencies.
- `uv run provgate --help` — CLI entry point.
- `uv run provgate keygen` — print a fresh Fernet master key for `PROVGATE_SECRET_KEY`.
- `uv run provgate class add|list|edit|remove` — manage class configs (secrets read from prompt/stdin, never argv).
- `uv run provgate doctor --class <label>` — verify a class's Gradescope login + Provenance token before trusting it.
- `uv run provgate sync [--all | --class <label>] [--dry-run]` — one sync pass.
- `uv run provgate runs` — recent sync history.
- `uv run pytest` — test suite (excludes `@pytest.mark.live`).
- `uv run pytest -m live` — live integration tests (requires real credentials in env).
- `uv run ruff check . && uv run ruff format --check .` — lint + format.
- `uv run mypy --strict src` — type check.

If you need a command that doesn't exist, ask before adding it.

## Repo layout

```
provenance-gradescope-gateway/
├── CLAUDE.md                 # this file
├── README.md                 # quickstart, install, configure a class, deploy
├── pyproject.toml            # deps, ruff/mypy/pytest config, console_scripts entry
├── Dockerfile                # container image for scheduled deployment
├── docs/
│   └── superpowers/specs/    # design specs
├── src/provgate/
│   ├── cli/                  # thin CLI frontend (later: web/)
│   ├── sync/                 # orchestration + pure delta/prune logic
│   ├── gradescope/           # authenticated Gradescope client (undocumented API, quarantined)
│   ├── provenance/           # Provenance HTTP API client
│   ├── store/                # SQLite repository + secret encryption
│   ├── notify/               # webhook summary: pure render + best-effort post
│   └── config.py             # settings
└── tests/                    # mirrors src/; fixtures for exports + fake Provenance
```

## Future / parked ideas

- **Upstream our bulk-export download to `gradescopeapi`.** We hand-roll the whole Gradescope client (login, listing, async bulk-export download) on `httpx` and do not depend on `gradescopeapi`. The library covers login/listing/upload but *not* the bulk-export download; once our download flow is proven here, proposing a PR upstream (or adopting the library for login/listing to shrink our own scraping-maintenance surface) is an option. Parked, not scheduled.

## When in doubt

Re-read the spec, re-read this file, and ask. The cost of a clarifying question is five minutes. The cost of building the wrong thing around an undocumented API is a week.
