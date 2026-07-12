"""Sync orchestration: per class → per in-scope assignment → delta → POST → poll.

Cross-class and cross-assignment failures are isolated: they are recorded as `runs`
and returned as outcomes, never raised past the class boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from provgate.store.models import ClassConfig, RunRecord, SecretKind
from provgate.store.repository import Repository

from .policy import resolve_assignments
from .ports import GradescopeLogin, GradescopePort, ProvenancePort
from .prune import prune_export


@dataclass(frozen=True)
class AssignmentOutcome:
    gs_assignment_id: str
    outcome: str  # succeeded | partial | failed | skipped | dry_run | error
    delta_count: int
    job_id: str | None
    error: str | None


def _record(
    repo: Repository, cfg: ClassConfig, o: AssignmentOutcome, started: str, finished: str
) -> None:
    repo.record_run(
        RunRecord(
            class_id=cfg.id,
            gs_assignment_id=o.gs_assignment_id,
            outcome=o.outcome,
            delta_count=o.delta_count,
            job_id=o.job_id,
            error_summary=o.error,
            started_at=started,
            finished_at=finished,
        )
    )


def _sync_assignment(
    repo: Repository,
    gs: GradescopePort,
    prov: ProvenancePort,
    cfg: ClassConfig,
    token: str,
    aid: str,
    *,
    now_iso: Callable[[], str],
    dry_run: bool,
) -> AssignmentOutcome:
    started = now_iso()
    export = gs.download_export(cfg.gradescope_course_id, aid)
    already = repo.forwarded_keys(cfg.id, aid)
    pruned = prune_export(export, already)
    delta = len(pruned.forwarded_keys)

    if delta == 0:
        out = AssignmentOutcome(aid, "skipped", 0, None, None)
        _record(repo, cfg, out, started, now_iso())
        return out

    if dry_run:
        out = AssignmentOutcome(aid, "dry_run", delta, None, None)
        _record(repo, cfg, out, started, now_iso())
        return out

    handle = prov.ingest_gradescope_export(
        cfg.provenance_base_url, token, cfg.provenance_semester_id, pruned.zip_bytes
    )
    status = prov.poll_job(
        cfg.provenance_base_url, token, cfg.provenance_semester_id, handle.job_id
    )
    if status.is_success:
        repo.mark_forwarded(cfg.id, aid, pruned.forwarded_keys, handle.job_id, now_iso())
        out = AssignmentOutcome(aid, status.status, delta, handle.job_id, None)
    else:
        out = AssignmentOutcome(aid, "failed", delta, handle.job_id, f"job status {status.status}")
    _record(repo, cfg, out, started, now_iso())
    return out


def sync_class(
    repo: Repository,
    gs_login: GradescopeLogin,
    prov: ProvenancePort,
    cfg: ClassConfig,
    *,
    now_iso: Callable[[], str],
    dry_run: bool = False,
) -> list[AssignmentOutcome]:
    started = now_iso()
    try:
        gs_pw = repo.get_secret(cfg.id, SecretKind.GRADESCOPE_PASSWORD)
        token = repo.get_secret(cfg.id, SecretKind.PROVENANCE_TOKEN)
        gs = gs_login(cfg.gradescope_email, gs_pw)
        all_ids = [a.id for a in gs.list_assignments(cfg.gradescope_course_id)]
        in_scope = resolve_assignments(cfg.assignment_policy, all_ids)
    except Exception as e:  # class-level failure: isolate, record, continue the pass
        out = AssignmentOutcome("*", "error", 0, None, str(e))
        _record(repo, cfg, out, started, now_iso())
        return [out]

    try:
        outcomes: list[AssignmentOutcome] = []
        for aid in in_scope:
            try:
                outcomes.append(
                    _sync_assignment(
                        repo, gs, prov, cfg, token, aid, now_iso=now_iso, dry_run=dry_run
                    )
                )
            except Exception as e:  # assignment-level failure: isolate from siblings
                out = AssignmentOutcome(aid, "error", 0, None, str(e))
                _record(repo, cfg, out, started, now_iso())
                outcomes.append(out)
        return outcomes
    finally:
        gs.close()


def sync_all(
    repo: Repository,
    gs_login: GradescopeLogin,
    prov: ProvenancePort,
    *,
    now_iso: Callable[[], str],
    dry_run: bool = False,
) -> dict[str, list[AssignmentOutcome]]:
    results: dict[str, list[AssignmentOutcome]] = {}
    for cfg in repo.list_classes(enabled_only=True):
        results[cfg.label] = sync_class(repo, gs_login, prov, cfg, now_iso=now_iso, dry_run=dry_run)
    return results
