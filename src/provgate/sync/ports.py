"""Structural interfaces the engine depends on (so it can be tested with fakes)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from provgate.gradescope.parse import Assignment
from provgate.provenance.client import JobHandle, JobStatus


class GradescopePort(Protocol):
    def list_assignments(self, course_id: str) -> list[Assignment]: ...
    def download_export(self, course_id: str, assignment_id: str) -> bytes: ...


GradescopeLogin = Callable[[str, str], GradescopePort]


class ProvenancePort(Protocol):
    def ingest_gradescope_export(
        self, base_url: str, token: str, semester_id: str, zip_bytes: bytes
    ) -> JobHandle: ...
    def poll_job(self, base_url: str, token: str, semester_id: str, job_id: str) -> JobStatus: ...
