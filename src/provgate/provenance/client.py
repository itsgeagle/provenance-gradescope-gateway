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


@dataclass(frozen=True)
class _ResumableUpload:
    upload_id: str
    s3_upload_id: str
    chunk_size: int
    total_parts: int


class ProvenanceClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        poll_interval_s: float = 2.0,
        poll_timeout_s: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        chunk_threshold_bytes: int = 16 * 1024 * 1024,
        chunk_size_bytes: int = 16 * 1024 * 1024,
        part_max_attempts: int = 4,
    ) -> None:
        self._http = http
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._chunk_threshold_bytes = chunk_threshold_bytes
        self._chunk_size_bytes = chunk_size_bytes
        self._part_max_attempts = part_max_attempts

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def ingest_gradescope_export(
        self, base_url: str, token: str, semester_id: str, zip_bytes: bytes
    ) -> JobHandle:
        if len(zip_bytes) < self._chunk_threshold_bytes:
            return self._ingest_single(base_url, token, semester_id, zip_bytes)
        return self._ingest_chunked(base_url, token, semester_id, zip_bytes)

    def _ingest_single(
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

    def _ingest_chunked(
        self, base_url: str, token: str, semester_id: str, zip_bytes: bytes
    ) -> JobHandle:
        upload = self._create_upload(base_url, token, semester_id, len(zip_bytes))
        chunk_size = upload.chunk_size
        for part_number in range(1, upload.total_parts + 1):
            start = (part_number - 1) * chunk_size
            end = min(start + chunk_size, len(zip_bytes))
            self._put_part(
                base_url,
                token,
                semester_id,
                upload.upload_id,
                upload.s3_upload_id,
                part_number,
                zip_bytes[start:end],
            )
        return self._complete_upload(
            base_url, token, semester_id, upload.upload_id, upload.s3_upload_id
        )

    def _create_upload(
        self, base_url: str, token: str, semester_id: str, total_bytes: int
    ) -> _ResumableUpload:
        url = f"{base_url}/semesters/{semester_id}/ingest/uploads"
        try:
            resp = self._http.post(
                url,
                headers=self._auth(token),
                json={
                    "filename": "export.zip",
                    "total_bytes": total_bytes,
                    "chunk_size": self._chunk_size_bytes,
                },
            )
        except httpx.HTTPError as e:
            raise ProvenanceError(f"create upload failed: {e}") from e
        if resp.status_code != 201:
            raise ProvenanceError(f"create upload returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise ProvenanceError(f"expected JSON response, got non-JSON body: {e}") from e
        upload_id = body.get("upload_id")
        s3_upload_id = body.get("s3_upload_id")
        chunk_size = body.get("chunk_size")
        total_parts = body.get("total_parts")
        if not upload_id or not s3_upload_id or not chunk_size or not total_parts:
            raise ProvenanceError("create upload response missing fields")
        return _ResumableUpload(
            upload_id=str(upload_id),
            s3_upload_id=str(s3_upload_id),
            chunk_size=int(chunk_size),
            total_parts=int(total_parts),
        )

    def _put_part(
        self,
        base_url: str,
        token: str,
        semester_id: str,
        upload_id: str,
        s3_upload_id: str,
        part_number: int,
        body: bytes,
    ) -> None:
        url = f"{base_url}/semesters/{semester_id}/ingest/uploads/{upload_id}/parts/{part_number}"
        params = {"s3_upload_id": s3_upload_id}
        last_error = f"part {part_number} failed"
        for attempt in range(self._part_max_attempts):
            try:
                resp = self._http.put(url, headers=self._auth(token), params=params, content=body)
                if resp.status_code == 200:
                    return
                last_error = f"part {part_number} returned {resp.status_code}"
            except httpx.HTTPError as e:
                last_error = f"part {part_number} request failed: {e}"
            if attempt < self._part_max_attempts - 1:
                self._sleep(0.5 * (2**attempt))
        raise ProvenanceError(last_error)

    def _complete_upload(
        self, base_url: str, token: str, semester_id: str, upload_id: str, s3_upload_id: str
    ) -> JobHandle:
        url = f"{base_url}/semesters/{semester_id}/ingest/uploads/{upload_id}/complete"
        try:
            resp = self._http.post(
                url, headers=self._auth(token), json={"s3_upload_id": s3_upload_id}
            )
        except httpx.HTTPError as e:
            raise ProvenanceError(f"complete upload failed: {e}") from e
        if resp.status_code != 202:
            raise ProvenanceError(f"complete upload returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise ProvenanceError(f"expected JSON response, got non-JSON body: {e}") from e
        job_id = body.get("job_id")
        if not job_id:
            raise ProvenanceError("complete response missing job_id")
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
