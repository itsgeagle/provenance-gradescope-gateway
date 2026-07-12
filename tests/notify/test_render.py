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
    assert (
        "totals: 1 classes · 40 pulled · 3 new ingested · 37 already synced · 0 classes failed"
        in out
    )


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


def test_dry_run_still_flags_failures_in_header() -> None:
    results = {
        "cs61a": [AssignmentOutcome("872677", "dry_run", 2, 10, None, None)],
        "cs61b": [AssignmentOutcome("*", "error", 0, 0, None, "login rejected")],
    }
    out = render_summary(results, now_iso=NOW, dry_run=True)
    assert "(dry run — nothing ingested)" in out
    assert "❌ 1 of 2 classes failed" in out


def test_totals_singular_for_one_failed_class() -> None:
    results = {"cs61b": [AssignmentOutcome("*", "error", 0, 0, None, "boom")]}
    out = render_summary(results, now_iso=NOW)
    # Anchor on the totals-line ending specifically: the header marker also
    # contains the literal substring "1 classes failed" (as part of "1 of 1
    # classes failed"), so a bare "not in out" check would false-fail here.
    assert "already synced · 1 class failed" in out
    assert "already synced · 1 classes failed" not in out


def test_classes_sorted_for_determinism() -> None:
    results = {
        "zeta": [_ok("1", 0, 5)],
        "alpha": [_ok("2", 0, 5)],
    }
    out = render_summary(results, now_iso=NOW)
    assert out.index("alpha") < out.index("zeta")
