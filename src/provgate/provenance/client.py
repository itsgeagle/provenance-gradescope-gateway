"""HTTP client for Provenance's public ingest API (the fixed 3-call interface)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

_TERMINAL = {"succeeded", "partial", "failed"}
_SUCCESS = {"succeeded", "partial"}


class ProvenanceError(Exception):
    """A Provenance API call failed or a job did not terminate in time."""


@dataclass(frozen=True)
class JobHandle:
    job_id: str


@dataclass(frozen=True)
class JobStatus:
    status: str
    raw: dict[str, object]

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def is_success(self) -> bool:
        return self.status in _SUCCESS


class ProvenanceClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        poll_interval_s: float = 2.0,
        poll_timeout_s: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self._sleep = sleep
        self._monotonic = monotonic

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def ingest_gradescope_export(
        self, base_url: str, token: str, semester_id: str, zip_bytes: bytes
    ) -> JobHandle:
        url = f"{base_url}/semesters/{semester_id}/ingest:gradescope"
        try:
            resp = self._http.post(
                url,
                headers=self._auth(token),
                files={"archive": ("export.zip", zip_bytes, "application/zip")},
            )
        except httpx.HTTPError as e:
            raise ProvenanceError(f"ingest request failed: {e}") from e
        if resp.status_code != 202:
            raise ProvenanceError(f"ingest returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise ProvenanceError(f"expected JSON response, got non-JSON body: {e}") from e
        job_id = body.get("job_id")
        if not job_id:
            raise ProvenanceError("ingest response missing job_id")
        return JobHandle(job_id=str(job_id))

    def verify_token(self, base_url: str, token: str) -> bool:
        url = f"{base_url}/me"
        try:
            resp = self._http.get(url, headers=self._auth(token))
        except httpx.HTTPError as e:
            raise ProvenanceError(f"token verification failed: {e}") from e
        if resp.status_code == 200:
            return True
        if resp.status_code in (401, 403):
            return False
        raise ProvenanceError(f"token verification returned {resp.status_code}")

    def poll_job(self, base_url: str, token: str, semester_id: str, job_id: str) -> JobStatus:
        url = f"{base_url}/semesters/{semester_id}/ingest/jobs/{job_id}"
        deadline = self._monotonic() + self._poll_timeout_s
        while True:
            try:
                resp = self._http.get(url, headers=self._auth(token))
            except httpx.HTTPError as e:
                raise ProvenanceError(f"job poll failed: {e}") from e
            if resp.status_code != 200:
                raise ProvenanceError(f"job poll returned {resp.status_code}")
            try:
                body = resp.json()
            except ValueError as e:
                raise ProvenanceError(f"expected JSON response, got non-JSON body: {e}") from e
            status = JobStatus(status=str(body.get("status", "")), raw=body)
            if status.is_terminal:
                return status
            if self._monotonic() >= deadline:
                raise ProvenanceError(f"job {job_id} did not terminate within timeout")
            self._sleep(self._poll_interval_s)
