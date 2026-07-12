from provgate.gradescope.parse import Assignment
from provgate.provenance.client import JobHandle, JobStatus
from provgate.store.crypto import SecretBox, generate_key
from provgate.store.db import connect
from provgate.store.models import AssignmentPolicy, PolicyKind, SecretKind
from provgate.store.repository import Repository
from provgate.sync.engine import sync_class
from tests.support.export_fixture import make_export

NOW = lambda: "2026-07-12T00:00:00Z"  # noqa: E731


def seed() -> tuple[Repository, int]:
    repo = Repository(connect(":memory:"), SecretBox(generate_key()))
    c = repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e@x",
        provenance_base_url="https://prov/api/v1",
        provenance_semester_id="sem",
        assignment_policy=AssignmentPolicy(PolicyKind.INCLUDE, ("2",)),
    )
    repo.set_secret(c.id, SecretKind.GRADESCOPE_PASSWORD, "pw")
    repo.set_secret(c.id, SecretKind.PROVENANCE_TOKEN, "tok")
    return repo, c.id


class FakeGs:
    def __init__(self, export: bytes) -> None:
        self._export = export

    def list_assignments(self, course_id: str) -> list[Assignment]:
        return [Assignment(id="2", title="HW"), Assignment(id="3", title="Other")]

    def download_export(self, course_id: str, assignment_id: str) -> bytes:
        return self._export


class FakeProv:
    def __init__(self, status: str = "succeeded") -> None:
        self.status = status
        self.ingested: list[bytes] = []

    def ingest_gradescope_export(self, base_url, token, semester_id, zip_bytes) -> JobHandle:
        self.ingested.append(zip_bytes)
        return JobHandle("job-1")

    def poll_job(self, base_url, token, semester_id, job_id) -> JobStatus:
        return JobStatus(self.status, {"status": self.status})


def test_success_advances_watermark_and_forwards_delta_only() -> None:
    repo, cid = seed()
    export = make_export(
        {
            "submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}},
            "submission_2": {"sid": "s2", "files": {"manifest.json": b"b"}},
        }
    )
    prov = FakeProv("succeeded")
    login = lambda email, pw: FakeGs(export)  # noqa: E731

    out1 = sync_class(repo, login, prov, repo.get_class("a"), now_iso=NOW)
    assert len(prov.ingested) == 1
    assert [o.outcome for o in out1] == ["succeeded"]
    assert out1[0].delta_count == 2
    assert repo.forwarded_keys(cid, "2") == {"submission_1", "submission_2"}

    # second run: nothing new → skipped, no new ingest
    out2 = sync_class(repo, login, prov, repo.get_class("a"), now_iso=NOW)
    assert len(prov.ingested) == 1
    assert out2[0].outcome == "skipped"


def test_failed_job_leaves_watermark_untouched() -> None:
    repo, cid = seed()
    export = make_export({"submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}}})
    prov = FakeProv("failed")
    login = lambda email, pw: FakeGs(export)  # noqa: E731
    out = sync_class(repo, login, prov, repo.get_class("a"), now_iso=NOW)
    assert out[0].outcome == "failed"
    assert repo.forwarded_keys(cid, "2") == set()


def test_dry_run_forwards_nothing() -> None:
    repo, cid = seed()
    export = make_export({"submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}}})
    prov = FakeProv("succeeded")
    login = lambda email, pw: FakeGs(export)  # noqa: E731
    out = sync_class(repo, login, prov, repo.get_class("a"), now_iso=NOW, dry_run=True)
    assert out[0].outcome == "dry_run"
    assert out[0].delta_count == 1
    assert prov.ingested == []
    assert repo.forwarded_keys(cid, "2") == set()


def test_login_failure_records_error_and_does_not_raise() -> None:
    repo, cid = seed()

    def boom(email: str, pw: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("gradescope down")

    out = sync_class(repo, boom, FakeProv(), repo.get_class("a"), now_iso=NOW)
    assert out[0].outcome == "error"
    assert repo.recent_runs()[0].outcome == "error"
