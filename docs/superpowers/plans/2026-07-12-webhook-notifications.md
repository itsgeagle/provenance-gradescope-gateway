# Webhook Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** After every `provgate sync` pass, POST a complete Discord/Slack-compatible summary (pulled / new / already-synced counts + failures) to a configured webhook URL — best-effort, never affecting sync correctness.

**Architecture:** A pure `render_summary` over the engine's `AssignmentOutcome` results + a best-effort `post_summary` webhook client, wired into the CLI `sync` command's per-pass work. The engine gains one additive field (`total_submissions`) so the summary can report "pulled" vs "new".

**Tech Stack:** Python 3.11+, httpx, Typer. Tests: pytest + respx.

## Global Constraints

- Python 3.11+; `mypy --strict src` clean; `ruff check` + `ruff format --check` clean.
- No new dependencies (httpx already present).
- Package `provgate`; source `src/provgate/`; tests mirror under `tests/`.
- Commits: conventional-commit prefix, `--no-gpg-sign`, no Co-Authored-By trailer, explicit pathspec.
- **Isolation invariant:** a webhook failure must NEVER raise, change a watermark/`runs` row/exit code, or alter any sync outcome. `post_summary` swallows all errors and returns a bool; the sync is fully complete before the summary is rendered.
- Webhook URL is deployment config (env), NOT a stored secret.

---

### Task 1: Add `total_submissions` to `AssignmentOutcome`

**Files:**
- Modify: `src/provgate/sync/engine.py`
- Test: `tests/sync/test_engine.py`

**Interfaces:**
- `AssignmentOutcome` gains `total_submissions: int` immediately after `delta_count`. New field order: `gs_assignment_id, outcome, delta_count, total_submissions, job_id, error`.

- [ ] **Step 1: Update the existing engine tests to assert the new field (RED)**

In `tests/sync/test_engine.py`, in `test_success_advances_watermark_and_forwards_delta_only`, after `assert out1[0].delta_count == 2`, add:
```python
    assert out1[0].total_submissions == 2  # both submissions pulled from the export
```
And in the same test's second-run block, after `assert out2[0].outcome == "skipped"`, add:
```python
    assert out2[0].total_submissions == 2  # still 2 pulled, 0 new
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/sync/test_engine.py -k test_success_advances -v`
Expected: FAIL (`AssignmentOutcome.__init__() takes ... positional arguments` or missing attribute).

- [ ] **Step 3: Add the field and populate it on every path**

In `src/provgate/sync/engine.py`:

Add the field to the dataclass:
```python
@dataclass(frozen=True)
class AssignmentOutcome:
    gs_assignment_id: str
    outcome: str  # succeeded | partial | failed | skipped | dry_run | error
    delta_count: int
    total_submissions: int
    job_id: str | None
    error: str | None
```

Update every construction site (positional order matches the field order above):
- skipped path: `out = AssignmentOutcome(aid, "skipped", 0, pruned.total_submissions, None, None)`
- dry_run path: `out = AssignmentOutcome(aid, "dry_run", delta, pruned.total_submissions, None, None)`
- success path: `out = AssignmentOutcome(aid, status.status, delta, pruned.total_submissions, handle.job_id, None)`
- failed path: `out = AssignmentOutcome(aid, "failed", delta, pruned.total_submissions, handle.job_id, f"job status {status.status}")`
- class-level error (in `sync_class`): `out = AssignmentOutcome("*", "error", 0, 0, None, str(e))`
- assignment-level error (in `sync_class` loop): `out = AssignmentOutcome(aid, "error", 0, 0, None, str(e))`

- [ ] **Step 4: Run the full engine suite**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/sync/test_engine.py -v && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src`
Expected: all engine tests pass; lint/format/types clean. (If format-check complains, `uv run ruff format .`.)

- [ ] **Step 5: Commit**

```bash
git add src/provgate/sync/engine.py tests/sync/test_engine.py
git commit --no-gpg-sign -m "feat(sync): carry total_submissions (pulled count) on AssignmentOutcome"
```

---

### Task 2: Pure summary renderer (`notify/render.py`)

**Files:**
- Create: `src/provgate/notify/__init__.py` (empty)
- Create: `src/provgate/notify/render.py`
- Test: `tests/notify/__init__.py` (empty), `tests/notify/test_render.py`

**Interfaces:**
- Consumes: `AssignmentOutcome` (Task 1).
- Produces: `def render_summary(results: dict[str, list[AssignmentOutcome]], *, now_iso: str, dry_run: bool = False) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/notify/__init__.py`: (empty file)

`tests/notify/test_render.py`:
```python
from provgate.notify.render import render_summary
from provgate.sync.engine import AssignmentOutcome

NOW = "2026-07-12T18:00:00Z"


def _ok(aid: str, new: int, pulled: int) -> AssignmentOutcome:
    return AssignmentOutcome(aid, "succeeded", new, pulled, "job-1", None)


def test_all_healthy_reports_counts_and_totals() -> None:
    results = {
        "cs61a": [_ok("872677", 2, 25), _ok("872690", 1, 15)],
    }
    out = render_summary(results, now_iso=NOW)
    assert out.startswith(f"**provgate sync** · {NOW} · ✅ all healthy")
    assert "cs61a — pulled 40, new 3 ingested, 37 already synced" in out
    assert "872677: 2 new/25" in out and "872690: 1 new/15" in out
    assert "totals: 1 classes · 40 pulled · 3 new ingested · 37 already synced · 0 classes failed" in out


def test_failure_is_flagged_in_header_and_line() -> None:
    results = {
        "cs61a": [_ok("872677", 3, 40)],
        "cs61b": [AssignmentOutcome("*", "error", 0, 0, None, "login rejected")],
    }
    out = render_summary(results, now_iso=NOW)
    assert "❌ 1 of 2 classes failed" in out
    assert "❌ cs61b — pulled 0, new 0 ingested, 0 already synced — login rejected" in out
    assert "✅ cs61a —" in out


def test_empty_results_says_no_classes() -> None:
    out = render_summary({}, now_iso=NOW)
    assert "✅ no classes configured" in out
    assert "totals: 0 classes" in out


def test_dry_run_marker() -> None:
    results = {"cs61a": [AssignmentOutcome("872677", "dry_run", 2, 10, None, None)]}
    out = render_summary(results, now_iso=NOW, dry_run=True)
    assert "(dry run — nothing ingested)" in out


def test_classes_sorted_for_determinism() -> None:
    results = {
        "zeta": [_ok("1", 0, 5)],
        "alpha": [_ok("2", 0, 5)],
    }
    out = render_summary(results, now_iso=NOW)
    assert out.index("alpha") < out.index("zeta")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/notify/test_render.py -v`
Expected: FAIL (`ModuleNotFoundError: provgate.notify`).

- [ ] **Step 3: Write `notify/render.py`**

```python
"""Render a sync pass's outcomes into a Discord/Slack-compatible summary string.

Pure: a deterministic function of (results, now_iso, dry_run). No clock, no I/O.
The POST body Provenance's webhook sink uses is `{"content": <this string>}`,
which both Discord and Slack-compatible incoming webhooks accept.
"""

from __future__ import annotations

from provgate.sync.engine import AssignmentOutcome

_FAIL = {"failed", "error"}


def _class_failed(outcomes: list[AssignmentOutcome]) -> bool:
    return any(o.outcome in _FAIL for o in outcomes)


def render_summary(
    results: dict[str, list[AssignmentOutcome]],
    *,
    now_iso: str,
    dry_run: bool = False,
) -> str:
    total_classes = len(results)
    failed_classes = sum(1 for outs in results.values() if _class_failed(outs))

    if dry_run:
        marker = "(dry run — nothing ingested)"
    elif total_classes == 0:
        marker = "✅ no classes configured"
    elif failed_classes:
        marker = f"❌ {failed_classes} of {total_classes} classes failed"
    else:
        marker = "✅ all healthy"

    lines = [f"**provgate sync** · {now_iso} · {marker}"]

    tot_pulled = tot_new = tot_already = 0
    for label in sorted(results):
        outcomes = results[label]
        pulled = sum(o.total_submissions for o in outcomes)
        new = sum(o.delta_count for o in outcomes)
        already = pulled - new
        tot_pulled += pulled
        tot_new += new
        tot_already += already

        failed = _class_failed(outcomes)
        prefix = "❌" if failed else "✅"
        line = f"{prefix} {label} — pulled {pulled}, new {new} ingested, {already} already synced"
        if failed:
            first = next(o for o in outcomes if o.outcome in _FAIL)
            line += f" — {first.error or first.outcome}"
        else:
            breakdown = ", ".join(
                f"{o.gs_assignment_id}: {o.delta_count} new/{o.total_submissions}"
                for o in outcomes
                if o.gs_assignment_id != "*"
            )
            if breakdown:
                line += f"  ({breakdown})"
        lines.append(line)

    lines.append(
        f"— totals: {total_classes} classes · {tot_pulled} pulled · "
        f"{tot_new} new ingested · {tot_already} already synced · "
        f"{failed_classes} classes failed"
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/notify/test_render.py -v && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src`
Expected: 5 passed; lint/format/types clean.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/notify/__init__.py src/provgate/notify/render.py tests/notify/__init__.py tests/notify/test_render.py
git commit --no-gpg-sign -m "feat(notify): pure sync-summary renderer"
```

---

### Task 3: Best-effort webhook POST (`notify/webhook.py`)

**Files:**
- Create: `src/provgate/notify/webhook.py`
- Test: `tests/notify/test_webhook.py`

**Interfaces:**
- Produces: `def post_summary(url: str, content: str, *, timeout_s: float, http: httpx.Client | None = None) -> bool` — POSTs `{"content": content}`; returns True on 2xx; catches ALL errors, logs a warning, returns False, never raises. Closes only a client it created (not an injected one).

- [ ] **Step 1: Write the failing test**

`tests/notify/test_webhook.py`:
```python
import httpx
import respx

from provgate.notify.webhook import post_summary

URL = "https://hooks.example.com/wh"


@respx.mock
def test_post_success_returns_true_and_sends_content() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ok = post_summary(URL, "hello", timeout_s=5.0, http=httpx.Client())
    assert ok is True
    assert route.calls.last.request.content == b'{"content": "hello"}' or b'"content"' in route.calls.last.request.content


@respx.mock
def test_non_2xx_returns_false_and_does_not_raise() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500))
    assert post_summary(URL, "x", timeout_s=5.0, http=httpx.Client()) is False


@respx.mock
def test_transport_error_returns_false_and_does_not_raise() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
    assert post_summary(URL, "x", timeout_s=5.0, http=httpx.Client()) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/notify/test_webhook.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `notify/webhook.py`**

```python
"""Best-effort webhook POST of a sync summary. Never raises.

A dead/slow/erroring webhook must never affect sync correctness — this returns a
bool and swallows every exception, logging at warning level. The caller ignores
the return value beyond logging.
"""

from __future__ import annotations

import logging

import httpx

_log = logging.getLogger("provgate.notify")


def post_summary(
    url: str,
    content: str,
    *,
    timeout_s: float,
    http: httpx.Client | None = None,
) -> bool:
    client = http if http is not None else httpx.Client(timeout=timeout_s)
    try:
        resp = client.post(url, json={"content": content})
        if resp.status_code // 100 == 2:
            return True
        _log.warning("webhook post returned HTTP %s", resp.status_code)
        return False
    except Exception as e:  # best-effort: a notify failure must never break a sync
        _log.warning("webhook post failed: %s", e)
        return False
    finally:
        if http is None:
            client.close()
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/notify/test_webhook.py -v && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src`
Expected: 3 passed; lint/format/types clean.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/notify/webhook.py tests/notify/test_webhook.py
git commit --no-gpg-sign -m "feat(notify): best-effort webhook POST that never raises"
```

---

### Task 4: Config + CLI wiring + docs

**Files:**
- Modify: `src/provgate/config.py`
- Modify: `src/provgate/cli/main.py`
- Modify: `README.md`, `CLAUDE.md`
- Test: `tests/test_config.py`, `tests/cli/test_cli.py`

**Interfaces:**
- `Settings` gains `webhook_url: str | None = None`, `webhook_timeout_s: float = 10.0`.
- `load_settings` reads `PROVGATE_WEBHOOK_URL` (empty/unset → None) and `PROVGATE_WEBHOOK_TIMEOUT_S` (float, default 10.0, via the existing optional-float helper).
- `sync`'s per-pass `_once()` posts a summary when `settings.webhook_url` is set.

- [ ] **Step 1: Read the current files first**

Read `src/provgate/config.py` (note the existing optional-float helper and how `load_settings` is structured) and `src/provgate/cli/main.py` (note the `sync` command's `_once()` closure and where `results` is built/echoed in both the `--class` and all-classes branches).

- [ ] **Step 2: Write failing tests**

Add to `tests/test_config.py`:
```python
def test_webhook_settings_default_and_override() -> None:
    base = {"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"}
    s = load_settings(base)
    assert s.webhook_url is None
    assert s.webhook_timeout_s == 10.0
    s2 = load_settings(
        {**base, "PROVGATE_WEBHOOK_URL": "https://h/wh", "PROVGATE_WEBHOOK_TIMEOUT_S": "3.5"}
    )
    assert s2.webhook_url == "https://h/wh"
    assert s2.webhook_timeout_s == 3.5
```

Add to `tests/cli/test_cli.py` (ensure `import json`, `import httpx`, `import respx` are present at the top):
```python
@respx.mock
def test_sync_posts_webhook_summary_when_url_set(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    route = respx.post("https://hooks.example.com/wh").mock(return_value=httpx.Response(204))
    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    monkeypatch.setenv("PROVGATE_WEBHOOK_URL", "https://hooks.example.com/wh")

    result = runner.invoke(app, ["sync", "--all"])
    assert result.exit_code == 0, result.stdout
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert "provgate sync" in body["content"]


@respx.mock
def test_sync_no_webhook_when_url_unset(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    route = respx.post("https://hooks.example.com/wh").mock(return_value=httpx.Response(204))
    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    # no PROVGATE_WEBHOOK_URL
    result = runner.invoke(app, ["sync", "--all"])
    assert result.exit_code == 0, result.stdout
    assert not route.called
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest tests/test_config.py -k webhook tests/cli/test_cli.py -k webhook -v`
Expected: FAIL (config lacks fields; sync posts nothing).

- [ ] **Step 4: Implement config fields**

In `src/provgate/config.py`, add to the `Settings` dataclass (after the existing timeout fields):
```python
    webhook_url: str | None = None
    webhook_timeout_s: float = 10.0
```
In `load_settings`, add to the returned `Settings(...)` (using the same optional-float helper already used for the other timeouts; the URL is `env.get("PROVGATE_WEBHOOK_URL") or None`):
```python
        webhook_url=env.get("PROVGATE_WEBHOOK_URL") or None,
        webhook_timeout_s=<optional-float helper>(env, "PROVGATE_WEBHOOK_TIMEOUT_S", 10.0),
```
(Match the exact name/signature of the existing optional-float helper in the file.)

- [ ] **Step 5: Wire the post into `sync`**

In `src/provgate/cli/main.py`, add near the other imports:
```python
from provgate.notify.render import render_summary
from provgate.notify.webhook import post_summary
```
Inside the `sync` command's `_once()` closure, after the loop that `typer.echo`s the per-class outcomes (so `results` is fully populated for both the `--class` and all-classes branches), add:
```python
        if settings.webhook_url:
            content = render_summary(results, now_iso=utc_now_iso(), dry_run=dry_run)
            post_summary(
                settings.webhook_url,
                content,
                timeout_s=settings.webhook_timeout_s,
            )
```
Ensure `results` in the `--class` branch is the same `{label: [outcomes]}` dict shape the all-classes branch produces (it already is). `settings` is already in scope in `sync`.

- [ ] **Step 6: Run the targeted + full suite**

Run: `cd /Users/aaryanmehta/projects/provenance-gradescope-gateway && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src`
Expected: full suite green (incl. the new config + CLI tests); lint/format/types clean.

- [ ] **Step 7: Docs**

In `README.md`, add a "Notifications" section after "Run a sync": explain that setting `PROVGATE_WEBHOOK_URL` (Discord/Slack-compatible incoming webhook) makes `provgate` POST a summary after every sync pass (pulled / new / already-synced counts + failures), that it's best-effort (a webhook failure never affects a sync), and document `PROVGATE_WEBHOOK_TIMEOUT_S` (default 10). Include the example message block from the spec.

In `CLAUDE.md`, under the module map / "Things we are explicitly not doing" area, add one line noting the `notify/` package (pure `render_summary` + best-effort `post_summary`) and that a webhook failure must never affect sync correctness (the isolation invariant).

- [ ] **Step 8: Commit**

```bash
git add src/provgate/config.py src/provgate/cli/main.py tests/test_config.py tests/cli/test_cli.py README.md CLAUDE.md
git commit --no-gpg-sign -m "feat(notify): post per-pass webhook summary from sync; config + docs"
```

---

## Self-Review

- **Spec coverage:** every-pass post (Task 4 `_once`), complete counts pulled/new/already (Task 1 field + Task 2 render), failures flagged (Task 2), global env config (Task 4), best-effort isolation (Task 3 + invariant), Discord/Slack `{"content"}` shape (Task 3), docs (Task 4). Covered.
- **Placeholder scan:** the only intentional "match the existing helper" reference is Task 4 Step 4 (optional-float helper) — the implementer is told to read the file first; not a placeholder for logic.
- **Type consistency:** `total_submissions` field order fixed in Task 1 and consumed positionally there and by name in render (Task 2); `render_summary`/`post_summary` signatures identical across tasks and the CLI call site.
