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
