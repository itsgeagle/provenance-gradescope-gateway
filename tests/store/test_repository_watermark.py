from provgate.store.crypto import SecretBox, generate_key
from provgate.store.db import connect
from provgate.store.models import AssignmentPolicy, PolicyKind, RunRecord
from provgate.store.repository import Repository


def make_repo_with_class() -> tuple[Repository, int]:
    repo = Repository(connect(":memory:"), SecretBox(generate_key()))
    c = repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    return repo, c.id


def test_watermark_starts_empty_and_accumulates() -> None:
    repo, cid = make_repo_with_class()
    assert repo.forwarded_keys(cid, "872677") == set()
    repo.mark_forwarded(
        cid, "872677", ["submission_1", "submission_2"], "job-1", "2026-07-12T00:00:00Z"
    )
    assert repo.forwarded_keys(cid, "872677") == {"submission_1", "submission_2"}
    # a different assignment is isolated
    assert repo.forwarded_keys(cid, "999") == set()


def test_mark_forwarded_is_idempotent() -> None:
    repo, cid = make_repo_with_class()
    repo.mark_forwarded(cid, "872677", ["submission_1"], "job-1", "2026-07-12T00:00:00Z")
    repo.mark_forwarded(cid, "872677", ["submission_1"], "job-2", "2026-07-12T01:00:00Z")
    assert repo.forwarded_keys(cid, "872677") == {"submission_1"}


def test_record_and_read_runs() -> None:
    repo, cid = make_repo_with_class()
    repo.record_run(
        RunRecord(
            class_id=cid,
            gs_assignment_id="872677",
            outcome="succeeded",
            delta_count=3,
            job_id="job-1",
            error_summary=None,
            started_at="2026-07-12T00:00:00Z",
            finished_at="2026-07-12T00:00:05Z",
        )
    )
    runs = repo.recent_runs()
    assert len(runs) == 1
    assert runs[0].outcome == "succeeded"
    assert runs[0].delta_count == 3
