# Design Spec — Webhook Notifications for `provgate`

**Date:** 2026-07-12
**Status:** Approved design, pre-implementation
**Depends on:** the shipped `provgate` sync gateway (store, sync engine, CLI).

## 1. Summary

After each sync pass, `provgate` POSTs a Discord/Slack-compatible summary of that
pass to a single configured webhook URL, so course staff hear about sync activity
and failures without reading logs or the `runs` table. Modeled on Provenance's
`notify` webhook sink (`packages/server/src/notify/`), scaled down to a per-run
summary.

## 2. Goals / non-goals

**Goals**
- One summary message per sync pass, covering every class in that pass, with
  failures prominently flagged.
- Global config via one env var; unset ⇒ feature off.
- **Best-effort and isolated:** a dead/slow/erroring webhook is logged and never
  fails, blocks, or delays the sync itself.
- True heartbeat: post on **every** pass (including "nothing new" no-ops) so a
  configured operator always knows the gateway ran.
- No I/O added to the sync engine — rendering is pure, posting happens in the CLI
  layer over the engine's existing returned outcomes.

**Non-goals (YAGNI vs. Provenance)**
- No SMTP/email sink.
- No per-event severity levels or min-severity gating (a per-run summary is
  already low-volume).
- No throttle/dedup engine.
- No message signing/HMAC.
- No per-class webhook routing (global URL only). All easy to add later.

## 3. Behavior

- If `PROVGATE_WEBHOOK_URL` is set, then at the end of every `sync` pass (each
  `--loop` iteration too; both `--all` and `--class`), `provgate` renders a
  summary from that pass's `AssignmentOutcome` results and POSTs it.
- The POST body is `{"content": "<summary>"}` — the exact shape Provenance's
  webhook sink uses, accepted by both Discord and Slack-compatible incoming
  webhooks.
- Posting is best-effort: a non-2xx response, timeout, or transport error is
  caught and logged at warning level; it never raises and never changes the sync
  outcome or exit code.
- `--dry-run` still posts a summary (clearly marked as a dry run), since the point
  is operator visibility; it just reports the computed delta with nothing ingested.

### Complete counts (the point of the summary)

The summary reports real numbers, not just status. Per assignment the engine knows:
- **pulled** — total submissions in the Gradescope export (`PrunedExport.total_submissions`).
- **new** — submissions forwarded this pass (`delta_count` = `len(forwarded_keys)`).
- **already synced** — `pulled − new` (skipped as duplicates / previously forwarded).
- **status** — `succeeded` / `partial` / `failed` / `skipped` / `dry_run` / `error`.

To surface "pulled" (not only "new"), `AssignmentOutcome` gains a
`total_submissions: int` field, set from `pruned.total_submissions` on every path
where the export was parsed (succeeded/partial/failed/skipped/dry_run); it is `0`
on an `error` outcome that failed before pruning. This is an additive field —
existing behavior and tests are unaffected.

Per class, counts are aggregated across its in-scope assignments; a grand-total
line aggregates across all classes.

### Message format (example)

```
**provgate sync** · 2026-07-12T18:00:00Z · ❌ 1 of 2 classes failed
✅ cs61a-fa26 — pulled 40, new 3 ingested, 37 already synced  (872677: 2 new/25, 872690: 1 new/15)
❌ cs61b-fa26 — pulled 12, new 0, LOGIN FAILED: login rejected (check credentials)
— totals: 2 classes · 52 pulled · 3 new ingested · 49 already synced · 1 class failed
```

- One header line: UTC timestamp + an at-a-glance failure marker (✅ all healthy /
  ❌ N of M classes failed). A `--dry-run` pass says `(dry run — nothing ingested)`.
- One line per class: ✅ if all its assignments succeeded/skipped, ❌ if any failed
  or errored (naming the failure), with its pulled / new / already-synced counts and
  a per-assignment breakdown.
- A final totals line summing pulled / new / already-synced / failed across all
  classes.
- Rendering is a pure function of `(results, now_iso, dry_run)` — deterministic, no
  clock or network inside it.

## 4. Config (env)

- `PROVGATE_WEBHOOK_URL` — optional. Unset/empty ⇒ notifications off.
- `PROVGATE_WEBHOOK_TIMEOUT_S` — float, default `10.0`.

Both added to `Settings` + `load_settings` (following the existing optional-float
pattern used for the poll/http timeouts). The webhook URL is **not** a secret in
the store — it's process/deployment config like the DB path, and lives in the
environment, not the encrypted `secrets` table.

## 5. Components

- `src/provgate/notify/__init__.py` — empty package marker.
- `src/provgate/sync/engine.py` — `AssignmentOutcome` gains `total_submissions: int`,
  populated from `pruned.total_submissions` on every parsed path (0 on pre-prune
  error). `sync_class`/`sync_all` signatures unchanged.
- `src/provgate/notify/render.py` — **pure**:
  `render_summary(results: dict[str, list[AssignmentOutcome]], *, now_iso: str, dry_run: bool = False) -> str`.
  Turns the engine's per-class outcomes into the Discord/Slack `content` string with
  pulled/new/already-synced counts + a totals line. Fully unit-tested: all-success,
  mixed success/failure, empty (no classes), dry-run, and multi-assignment count
  aggregation.
- `src/provgate/notify/webhook.py` —
  `post_summary(url: str, content: str, *, timeout_s: float, http: httpx.Client | None = None) -> bool`.
  POSTs `{"content": content}`; returns True on 2xx, False otherwise; **catches all
  errors, logs a warning, never raises**. Injectable `http` for respx tests.
- `src/provgate/config.py` — add `webhook_url: str | None`, `webhook_timeout_s: float`
  to `Settings`; read them in `load_settings`.
- `src/provgate/cli/main.py` — in the `sync` command's per-pass `_once()`, after
  `results` is computed and echoed, if `settings.webhook_url` is set, call
  `post_summary(settings.webhook_url, render_summary(results, now_iso=utc_now_iso()),
  timeout_s=settings.webhook_timeout_s)`. For a `--class` run, wrap the single-class
  outcome list into the same `{label: [...]}` dict shape before rendering.
- Docs: README "Notifications" section; CLAUDE.md note; `.env`-style example.

## 6. Testing

- `render_summary`: pure unit tests — deterministic, injected `now_iso`, cover
  success / failure / mixed / empty / dry-run / multi-assignment counts.
- `post_summary`: respx-mocked — 2xx → True; non-2xx → False + logged, no raise;
  transport error/timeout → False + logged, no raise (assert it never propagates).
- CLI: a test that `sync` with `PROVGATE_WEBHOOK_URL` set (respx-mocked) posts one
  summary, and that with it unset, nothing is posted. Zero-class pass still posts a
  (minimal) heartbeat when the URL is set.

## 7. Isolation guarantee (the load-bearing property)

The webhook must never affect sync correctness. `post_summary` swallows every
exception and returns a bool; the CLI ignores the return value beyond logging.
No webhook failure can change a watermark, a `runs` row, an exit code, or the
outcome of any class — the sync has fully completed before the summary is even
rendered.
