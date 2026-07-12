# provenance-gradescope-gateway (`provgate`)

A small Python service that syncs newly-submitted student work from **Gradescope** into a **[Provenance](https://github.com/)** server on a schedule.

Students record their work with the Provenance recorder and upload the resulting bundle to Gradescope as their submission. `provgate` runs hourly, pulls each configured assignment's new submissions out of Gradescope, and forwards them to Provenance's ingest API — so course staff see provenance analysis without anyone manually downloading and re-uploading exports.

> **Status:** pre-implementation. This README documents the intended design and interface. See [`docs/superpowers/specs/`](docs/superpowers/specs/) for the design spec.

---

## What it does

- **Multi-class.** Register any number of classes. Each has its own Gradescope login + course and its own target Provenance semester + API token. One sync pass services all of them; one class failing never blocks the others.
- **Incremental.** It forwards only *new* submissions each run (tracked by a per-assignment watermark), so it doesn't re-process the whole cohort every hour.
- **Zero Provenance changes.** It talks to Provenance purely over the public HTTP API (`ingest:gradescope` + job polling) with a normal API token.
- **Correct by construction.** The watermark is only an optimization — Provenance dedups submissions by content hash, so no duplicates are ever created even if a run repeats.

## How it works

```
 Gradescope                         provgate (hourly)                    Provenance
 ──────────                         ─────────────────                    ──────────
 student uploads      ── export ──▶ 1. log in (per class)
 recorder bundle;     without_       2. list in-scope assignments
 GS auto-extracts it  evaluations    3. download bulk export ZIP
                                     4. prune to NEW submissions ──POST──▶ /ingest:gradescope
                                        (metadata copied verbatim,          (parses, dedups,
                                         only new folders kept)              rosters, analyzes)
                                     5. poll job ─────────────────────────▶ /ingest/jobs/{id}
                                     6. on success: advance watermark  ◀─── succeeded/partial/failed
```

The Gradescope bulk export (`…/assignments/{id}/export/without_evaluations`) is already in the exact shape Provenance's `ingest:gradescope` endpoint expects — `submission_metadata.yml` plus one folder per submission — so no reformatting is needed. `provgate` only *filters* the export down to submissions it hasn't sent yet.

## Requirements

- Python 3.11+ (managed with [`uv`](https://docs.astral.sh/uv/)).
- A **Gradescope-native** staff account (email + password, **not** SSO-only, no 2FA) added as instructor/TA to each course you sync. SSO/2FA accounts are not supported — create a dedicated native account if needed.
- A Provenance **write-scoped** API token for each target semester (see the Provenance "API tokens" UI). Restrict it to just the semester it feeds.
- Network egress from wherever this runs to `gradescope.com` and to your Provenance server.

## Install

```bash
git clone <this-repo> && cd provenance-gradescope-gateway
uv sync
uv run provgate --help
```

## Configure the encryption key

All stored credentials (Gradescope passwords, Provenance tokens) are **encrypted at rest** in a local SQLite store. Provide the master key via environment:

```bash
export PROVGATE_SECRET_KEY="$(uv run provgate keygen)"   # generate once; store securely
export PROVGATE_DB_PATH="/var/lib/provgate/provgate.db"  # persistent location
```

Keep `PROVGATE_SECRET_KEY` out of git and out of the container image. Losing it means re-entering every class's credentials.

## Register a class

Secrets are prompted (or read from stdin/env) — never passed as command-line flags.

```bash
uv run provgate class add \
  --label "cs61a-fa26" \
  --gradescope-course 180852 \
  --provenance-base-url https://provenance.example.edu/api/v1 \
  --provenance-semester <semester-uuid> \
  --assignments all                       # or: --assignments include:872677,872690
                                          #     or: --assignments exclude:900001
# → prompts: Gradescope email, Gradescope password, Provenance API token
```

Assignment scope per class:

| `--assignments`        | Meaning                                                    |
| ---------------------- | ---------------------------------------------------------- |
| `all`                  | Every assignment in the course.                            |
| `include:<id>,<id>`    | Only these Gradescope assignment ids.                      |
| `exclude:<id>,<id>`    | Every assignment except these.                             |

Manage classes with `provgate class list`, `provgate class edit <label>`, `provgate class remove <label>`.

## Verify before trusting

```bash
uv run provgate doctor --class cs61a-fa26
# checks: Gradescope login works, course is visible, in-scope assignments resolve,
#         Provenance token is valid and write-scoped for the semester.
```

## Run a sync

```bash
uv run provgate sync --all              # every enabled class
uv run provgate sync --class cs61a-fa26 # one class
uv run provgate sync --all --dry-run    # compute the delta and report, but POST nothing
uv run provgate runs                     # recent sync history + outcomes
```

## Deploy (hourly)

`provgate sync --all` is a one-shot process: it runs one pass and exits. Drive the cadence with whatever scheduler your host provides.

**Container + external scheduler (recommended):**

```bash
docker build -t provgate .
# then invoke hourly via the platform's cron/scheduled task, e.g.:
docker run --rm \
  -e PROVGATE_SECRET_KEY \
  -e PROVGATE_DB_PATH=/data/provgate.db \
  -v provgate-data:/data \
  provgate sync --all
```

Mount a **persistent volume** for the SQLite store (`PROVGATE_DB_PATH`) so watermarks survive restarts.

**No external scheduler?** Use the built-in loop as a fallback:

```bash
docker run -d --restart=unless-stopped ... provgate sync --all --loop --interval 1h
```

## Security notes

- Credentials are encrypted at rest with a key that lives only in the environment, never in the DB, image, or git.
- Secrets never appear in command-line arguments, logs, or the run-history audit.
- Only `export/without_evaluations` is fetched — raw student submissions, no grades or rubric data.
- Fetched exports are streamed to Provenance and not retained after the run.

## Design & conventions

- Design spec: [`docs/superpowers/specs/`](docs/superpowers/specs/).
- Contributor conventions and architecture rules: [`CLAUDE.md`](CLAUDE.md). Read it before contributing — this project deliberately quarantines all undocumented-Gradescope-API fragility in one module and treats the Provenance HTTP contract as fixed.

## Roadmap / parked

- **Web GUI** for click-to-configure classes and view sync status. The core (`store` + `sync`) is already frontend-agnostic so this bolts on without reworking sync logic.
- **Upstream PR to [`gradescopeapi`](https://github.com/nyuoss/gradescope-api)** adding a submission-export download path. That library provides login/listing/upload but not submission download, so we implement export download here; once proven, contribute it back.
