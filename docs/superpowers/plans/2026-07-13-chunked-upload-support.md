# Chunked Upload Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `provgate` upload a pruned Gradescope export to Provenance via the resumable (chunked) S3-multipart endpoints when the export exceeds a size threshold, so deltas larger than the Provenance reverse-proxy's ~20 MiB `client_max_body_size` cap no longer `413`.

**Architecture:** All changes are quarantined behind the existing `ProvenanceClient.ingest_gradescope_export(base_url, token, semester_id, zip_bytes) -> JobHandle` seam. Internally it branches on `len(zip_bytes)`: below the threshold it uses today's single multipart POST unchanged; at/above it drives `POST /ingest/uploads` → `PUT …/parts/{n}` → `POST …/complete`, with per-part retry and a best-effort `DELETE` abort on unrecoverable failure. Two new `Settings` fields configure the threshold and chunk size. The engine, store, `ports` Protocol, and notify are untouched.

**Tech Stack:** Python 3.11+, `httpx` (sync `Client`), `respx` for HTTP mocking, `pytest`, `mypy --strict`, `ruff`. Managed with `uv`.

**Reference spec:** `docs/superpowers/specs/2026-07-13-chunked-upload-support-design.md`

## Global Constraints

- Python 3.11+. `mypy --strict` clean — no new `Any` except at the existing HTTP-boundary pattern (`resp.json()` results, exactly as the current client already does).
- `ruff check` and `ruff format --check` clean.
- Secrets never logged: the Bearer token, `s3_upload_id` (a capability secret), and part bodies must never appear in logs, exceptions, or `runs` rows. This plan adds no logging.
- The delta/watermark invariant is unchanged: the watermark advances only after `poll_job` returns `succeeded`/`partial`. Chunking is pure transport.
- Do NOT modify `sync/engine.py`, `sync/ports.py`, `store/`, or `notify/`. The port signature and `-> JobHandle` return are fixed.
- Do NOT change the byte content of the pruned ZIP; chunking splits the already-built bytes at arbitrary boundaries and the server reassembles them.
- Only two new config settings, both with defaults (16 MiB). No cross-pass resume, no store state, no progress reporting (YAGNI per spec).
- Commands run under `uv` (e.g. `uv run pytest …`, `uv run mypy --strict src`).

---

### Task 1: Config settings for threshold and chunk size

**Files:**
- Modify: `src/provgate/config.py` (Settings dataclass lines 15-23; add an int loader near `_optional_float` at lines 33-37; extend `load_settings` at lines 43-51)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.ingest_chunk_threshold_bytes: int` (default `16 * 1024 * 1024`) and `Settings.ingest_chunk_size_bytes: int` (default `16 * 1024 * 1024`), loaded from `PROVGATE_INGEST_CHUNK_THRESHOLD_BYTES` and `PROVGATE_INGEST_CHUNK_SIZE_BYTES`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_chunk_settings_default_and_override() -> None:
    base = {"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"}
    s = load_settings(base)
    assert s.ingest_chunk_threshold_bytes == 16 * 1024 * 1024
    assert s.ingest_chunk_size_bytes == 16 * 1024 * 1024
    s2 = load_settings(
        {
            **base,
            "PROVGATE_INGEST_CHUNK_THRESHOLD_BYTES": "1048576",
            "PROVGATE_INGEST_CHUNK_SIZE_BYTES": "524288",
        }
    )
    assert s2.ingest_chunk_threshold_bytes == 1048576
    assert s2.ingest_chunk_size_bytes == 524288
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_chunk_settings_default_and_override -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'ingest_chunk_threshold_bytes'`

- [ ] **Step 3: Add the int loader helper**

In `src/provgate/config.py`, add directly below `_optional_float` (after line 37):

```python
def _optional_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    if not value:
        return default
    return int(value)
```

- [ ] **Step 4: Add the two Settings fields**

In `src/provgate/config.py`, add to the `Settings` dataclass after `webhook_timeout_s` (line 23):

```python
    ingest_chunk_threshold_bytes: int = 16 * 1024 * 1024
    ingest_chunk_size_bytes: int = 16 * 1024 * 1024
```

- [ ] **Step 5: Wire the fields in load_settings**

In `src/provgate/config.py`, add inside the `Settings(...)` constructor in `load_settings` (before the closing `)` at line 51):

```python
        ingest_chunk_threshold_bytes=_optional_int(
            env, "PROVGATE_INGEST_CHUNK_THRESHOLD_BYTES", 16 * 1024 * 1024
        ),
        ingest_chunk_size_bytes=_optional_int(
            env, "PROVGATE_INGEST_CHUNK_SIZE_BYTES", 16 * 1024 * 1024
        ),
```

Note: the file comment "Fallbacks below must track the Settings dataclass defaults" (line 42) — the `16 * 1024 * 1024` defaults match the dataclass defaults above. Keep them in sync.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all config tests, including the new one)

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check src/provgate/config.py tests/test_config.py && uv run ruff format --check src/provgate/config.py tests/test_config.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add src/provgate/config.py tests/test_config.py
git commit -m "feat(config): add ingest chunk threshold + size settings"
```

---

### Task 2: Chunked upload happy path in ProvenanceClient

**Files:**
- Modify: `src/provgate/provenance/client.py` (add `_ResumableUpload` dataclass; extend `ProvenanceClient.__init__` at lines 38-52; convert `ingest_gradescope_export` at lines 58-79 into a size-branching dispatcher plus `_ingest_single`; add `_create_upload`, `_ingest_chunked`, `_put_part`, `_complete_upload`)
- Test: `tests/provenance/test_client.py`

**Interfaces:**
- Consumes: `Settings.ingest_chunk_threshold_bytes`, `Settings.ingest_chunk_size_bytes` (Task 1), passed as constructor kwargs.
- Produces: `ProvenanceClient(__init__(..., chunk_threshold_bytes: int = 16*1024*1024, chunk_size_bytes: int = 16*1024*1024, part_max_attempts: int = 4))`; private `_create_upload(...) -> _ResumableUpload`, `_put_part(...) -> None`, `_complete_upload(...) -> JobHandle`, `_ingest_single(...) -> JobHandle`, `_ingest_chunked(...) -> JobHandle`. `ingest_gradescope_export` keeps its exact signature and `-> JobHandle` return.
- `_ResumableUpload` fields: `upload_id: str`, `s3_upload_id: str`, `chunk_size: int`, `total_parts: int`.

- [ ] **Step 1: Write the failing tests**

Add to the top of `tests/provenance/test_client.py`, extend the imports and the `make_client` helper, then add the new tests. Replace the existing `make_client` (lines 10-19) with this parametrized version (existing no-arg callers still work because the defaults keep small payloads on the single-POST path):

```python
import re

def make_client(
    *,
    chunk_threshold_bytes: int = 16 * 1024 * 1024,
    chunk_size_bytes: int = 16 * 1024 * 1024,
    part_max_attempts: int = 4,
) -> ProvenanceClient:
    # sleep is a no-op; monotonic advances so timeout logic is deterministic
    ticks = iter(range(0, 10_000, 1))
    return ProvenanceClient(
        httpx.Client(),
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
        chunk_threshold_bytes=chunk_threshold_bytes,
        chunk_size_bytes=chunk_size_bytes,
        part_max_attempts=part_max_attempts,
    )
```

Add these tests at the end of the file:

```python
_PARTS_RE = rf"{re.escape(BASE)}/semesters/sem-1/ingest/uploads/up-1/parts/\d+"


@respx.mock
def test_small_payload_uses_single_post_not_chunked() -> None:
    single = respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "job-1"})
    )
    uploads = respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(201, json={})
    )
    handle = make_client(chunk_threshold_bytes=1000).ingest_gradescope_export(
        BASE, "tok", "sem-1", b"small"
    )
    assert handle.job_id == "job-1"
    assert single.called
    assert not uploads.called


@respx.mock
def test_large_payload_is_chunked_and_reassembles() -> None:
    payload = b"abcdefghij"  # 10 bytes
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 3,
            },
        )
    )
    parts = respx.put(url__regex=_PARTS_RE).mock(
        return_value=httpx.Response(200, json={"part_number": 1, "received": True})
    )
    complete = respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-42"})
    )

    handle = make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
        BASE, "tok", "sem-1", payload
    )

    assert handle.job_id == "job-42"
    assert parts.call_count == 3
    # Parts, reassembled in part-number order, reproduce the original bytes.
    ordered = sorted(parts.calls, key=lambda c: int(c.request.url.path.rsplit("/", 1)[1]))
    assert b"".join(c.request.content for c in ordered) == payload
    # Every part carries the s3_upload_id and the auth header.
    assert parts.calls.last.request.url.params["s3_upload_id"] == "s3-1"
    assert parts.calls.last.request.headers["authorization"] == "Bearer tok"
    assert complete.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_uses_server_returned_chunk_size_not_requested() -> None:
    payload = b"x" * 10
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 5,  # server clamps/overrides our requested 4
                "total_parts": 2,
            },
        )
    )
    parts = respx.put(url__regex=_PARTS_RE).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-7"})
    )

    make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
        BASE, "tok", "sem-1", payload
    )

    # Honored the server's chunk_size=5 (=> 2 parts), not our requested 4 (=> 3 parts).
    assert parts.call_count == 2
    first = min(parts.calls, key=lambda c: int(c.request.url.path.rsplit("/", 1)[1]))
    assert len(first.request.content) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/provenance/test_client.py -k "chunk or single_post or reassemble" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'chunk_threshold_bytes'`

- [ ] **Step 3: Add the `_ResumableUpload` dataclass**

In `src/provgate/provenance/client.py`, add after the `JobStatus` dataclass (after line 35):

```python
@dataclass(frozen=True)
class _ResumableUpload:
    upload_id: str
    s3_upload_id: str
    chunk_size: int
    total_parts: int
```

- [ ] **Step 4: Extend the constructor**

In `src/provgate/provenance/client.py`, replace the `__init__` (lines 39-52) with:

```python
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
```

- [ ] **Step 5: Convert `ingest_gradescope_export` into a dispatcher + `_ingest_single`**

In `src/provgate/provenance/client.py`, replace the whole `ingest_gradescope_export` method (lines 58-79) with:

```python
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
```

- [ ] **Step 6: Add `_ingest_chunked`, `_create_upload`, `_put_part`, `_complete_upload`**

In `src/provgate/provenance/client.py`, add these methods after `_ingest_single`:

```python
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
        url = (
            f"{base_url}/semesters/{semester_id}/ingest/uploads/{upload_id}"
            f"/parts/{part_number}"
        )
        params = {"s3_upload_id": s3_upload_id}
        last_error = f"part {part_number} failed"
        for attempt in range(self._part_max_attempts):
            try:
                resp = self._http.put(
                    url, headers=self._auth(token), params=params, content=body
                )
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
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `uv run pytest tests/provenance/test_client.py -k "chunk or single_post or reassemble" -v`
Expected: PASS

- [ ] **Step 8: Run the full client test module (regression: existing single-POST tests still green)**

Run: `uv run pytest tests/provenance/test_client.py -v`
Expected: PASS (all tests — the pre-existing `test_ingest_returns_job_handle`, `test_ingest_non_202_raises`, `test_ingest_non_json_body_raises_provenance_error` still pass because small payloads take the single-POST path)

- [ ] **Step 9: Lint and type-check**

Run: `uv run ruff check src/provgate/provenance/client.py tests/provenance/test_client.py && uv run ruff format --check src/provgate/provenance/client.py tests/provenance/test_client.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 10: Commit**

```bash
git add src/provgate/provenance/client.py tests/provenance/test_client.py
git commit -m "feat(provenance): chunked resumable upload for large exports"
```

---

### Task 3: Per-part retry on transient failure

**Files:**
- Modify: none (the retry loop was written in Task 2, Step 6 — this task adds the test that pins its behavior)
- Test: `tests/provenance/test_client.py`

**Interfaces:**
- Consumes: `_put_part` retry loop and the injected `sleep` from Task 2.
- Produces: nothing new; verifies backoff timing and eventual success.

- [ ] **Step 1: Write the failing test**

Add to `tests/provenance/test_client.py`:

```python
@respx.mock
def test_part_retried_on_transient_failure_with_backoff() -> None:
    payload = b"abcd"  # one 4-byte part
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 1,
            },
        )
    )
    parts = respx.put(url__regex=_PARTS_RE).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={}),
        ]
    )
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-9"})
    )

    sleeps: list[float] = []
    client = ProvenanceClient(
        httpx.Client(),
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        chunk_threshold_bytes=4,
        chunk_size_bytes=4,
        part_max_attempts=4,
    )

    handle = client.ingest_gradescope_export(BASE, "tok", "sem-1", payload)

    assert handle.job_id == "job-9"
    assert parts.call_count == 3  # two 500s, then success
    assert sleeps == [0.5, 1.0]  # exponential backoff after attempts 0 and 1
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/provenance/test_client.py::test_part_retried_on_transient_failure_with_backoff -v`
Expected: PASS (behavior implemented in Task 2). If it FAILS, the retry loop in `_put_part` is wrong — fix `_put_part`, do not weaken the test.

- [ ] **Step 3: Lint and type-check**

Run: `uv run ruff check tests/provenance/test_client.py && uv run ruff format --check tests/provenance/test_client.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add tests/provenance/test_client.py
git commit -m "test(provenance): pin per-part retry backoff on transient failure"
```

---

### Task 4: Abort on unrecoverable failure + error propagation

**Files:**
- Modify: `src/provgate/provenance/client.py` (wrap the parts+complete flow in `_ingest_chunked` from Task 2 with a best-effort abort; add `_abort_upload`)
- Test: `tests/provenance/test_client.py`

**Interfaces:**
- Consumes: `_ingest_chunked`, `_put_part`, `_complete_upload` from Task 2.
- Produces: `_abort_upload(base_url, token, semester_id, upload_id, s3_upload_id) -> None` (best-effort; swallows `httpx.HTTPError`). `_ingest_chunked` now `DELETE`s the upload on any `ProvenanceError` before re-raising.

- [ ] **Step 1: Write the failing tests**

Add to `tests/provenance/test_client.py`:

```python
@respx.mock
def test_part_exhaustion_aborts_upload_and_raises() -> None:
    payload = b"abcd"
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 1,
            },
        )
    )
    respx.put(url__regex=_PARTS_RE).mock(return_value=httpx.Response(500))
    abort = respx.delete(f"{BASE}/semesters/sem-1/ingest/uploads/up-1").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ProvenanceError):
        make_client(
            chunk_threshold_bytes=4, chunk_size_bytes=4, part_max_attempts=2
        ).ingest_gradescope_export(BASE, "tok", "sem-1", payload)

    assert abort.called
    assert abort.calls.last.request.url.params["s3_upload_id"] == "s3-1"


@respx.mock
def test_complete_failure_aborts_upload_and_raises() -> None:
    payload = b"abcd"
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 1,
            },
        )
    )
    respx.put(url__regex=_PARTS_RE).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(500)
    )
    abort = respx.delete(f"{BASE}/semesters/sem-1/ingest/uploads/up-1").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ProvenanceError):
        make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
            BASE, "tok", "sem-1", payload
        )

    assert abort.called


@respx.mock
def test_create_upload_failure_raises_without_abort() -> None:
    payload = b"abcd"
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(500)
    )
    abort = respx.delete(url__regex=rf"{re.escape(BASE)}/semesters/sem-1/ingest/uploads/.*").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ProvenanceError):
        make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
            BASE, "tok", "sem-1", payload
        )

    # No upload was created, so nothing to abort.
    assert not abort.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/provenance/test_client.py -k "abort or create_upload_failure" -v`
Expected: FAIL — `test_part_exhaustion_aborts_upload_and_raises` and `test_complete_failure_aborts_upload_and_raises` fail because no `DELETE` is issued (`abort.called` is `False`). `test_create_upload_failure_raises_without_abort` may already pass.

- [ ] **Step 3: Wrap the chunked flow with abort and add `_abort_upload`**

In `src/provgate/provenance/client.py`, replace the `_ingest_chunked` body (from Task 2, Step 6) with the version that aborts on failure:

```python
    def _ingest_chunked(
        self, base_url: str, token: str, semester_id: str, zip_bytes: bytes
    ) -> JobHandle:
        upload = self._create_upload(base_url, token, semester_id, len(zip_bytes))
        try:
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
        except ProvenanceError:
            # Best-effort cleanup so no orphaned S3 multipart parts linger. The
            # abort is cleanup, not correctness: it must not mask the original error.
            self._abort_upload(
                base_url, token, semester_id, upload.upload_id, upload.s3_upload_id
            )
            raise
```

Then add `_abort_upload` after `_complete_upload`:

```python
    def _abort_upload(
        self, base_url: str, token: str, semester_id: str, upload_id: str, s3_upload_id: str
    ) -> None:
        url = f"{base_url}/semesters/{semester_id}/ingest/uploads/{upload_id}"
        try:
            self._http.delete(
                url, headers=self._auth(token), params={"s3_upload_id": s3_upload_id}
            )
        except httpx.HTTPError:
            pass  # cleanup, not correctness — never let abort failure mask the real error
```

Note: `_create_upload` is called outside the `try`, so a create failure raises with no upload to abort — matching `test_create_upload_failure_raises_without_abort`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/provenance/test_client.py -k "abort or create_upload_failure" -v`
Expected: PASS

- [ ] **Step 5: Run the full client module (regression)**

Run: `uv run pytest tests/provenance/test_client.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/provgate/provenance/client.py tests/provenance/test_client.py && uv run ruff format --check src/provgate/provenance/client.py tests/provenance/test_client.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/provgate/provenance/client.py tests/provenance/test_client.py
git commit -m "feat(provenance): abort resumable upload on unrecoverable failure"
```

---

### Task 5: Wire the config settings into the real client

**Files:**
- Modify: `src/provgate/cli/wiring.py` (`real_prov` at lines 31-36)
- Test: `tests/cli/test_wiring.py` (new file)

**Interfaces:**
- Consumes: `Settings.ingest_chunk_threshold_bytes`, `Settings.ingest_chunk_size_bytes` (Task 1); `ProvenanceClient` chunk kwargs (Task 2).
- Produces: `real_prov(settings)` now passes both chunk settings to the `ProvenanceClient` constructor, so production reads them from env.

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_wiring.py`:

```python
import re
from pathlib import Path

import httpx
import respx

from provgate.cli.wiring import real_prov
from provgate.config import Settings

BASE = "https://prov.example.edu/api/v1"


def _settings() -> Settings:
    # Tiny threshold so a small payload takes the chunked path, proving the
    # setting flows through real_prov into the client.
    return Settings(
        db_path=Path("/tmp/x.db"),
        secret_key="k",
        ingest_chunk_threshold_bytes=4,
        ingest_chunk_size_bytes=4,
    )


@respx.mock
def test_real_prov_forwards_chunk_settings() -> None:
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 2,
            },
        )
    )
    parts = respx.put(
        url__regex=rf"{re.escape(BASE)}/semesters/sem-1/ingest/uploads/up-1/parts/\d+"
    ).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-1"})
    )

    prov = real_prov(_settings())
    handle = prov.ingest_gradescope_export(BASE, "tok", "sem-1", b"abcdefgh")  # 8 bytes > 4

    assert handle.job_id == "job-1"
    assert parts.call_count == 2  # chunked path was taken because threshold=4 flowed through
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_wiring.py -v`
Expected: FAIL — `parts.call_count` is 0 and the single-POST endpoint is hit instead, because `real_prov` still uses the 16 MiB default threshold (8-byte payload stays on the single-POST path).

- [ ] **Step 3: Forward the settings in `real_prov`**

In `src/provgate/cli/wiring.py`, replace `real_prov` (lines 31-36) with:

```python
def real_prov(settings: Settings) -> ProvenanceClient:
    return ProvenanceClient(
        httpx.Client(timeout=settings.http_timeout_s),
        poll_interval_s=settings.poll_interval_s,
        poll_timeout_s=settings.poll_timeout_s,
        chunk_threshold_bytes=settings.ingest_chunk_threshold_bytes,
        chunk_size_bytes=settings.ingest_chunk_size_bytes,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cli/test_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/provgate/cli/wiring.py tests/cli/test_wiring.py && uv run ruff format --check src/provgate/cli/wiring.py tests/cli/test_wiring.py && uv run mypy --strict src`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/provgate/cli/wiring.py tests/cli/test_wiring.py
git commit -m "feat(cli): wire ingest chunk settings into the Provenance client"
```

---

### Task 6: Full-suite regression and docs touch-up

**Files:**
- Modify: `README.md` (add the two new env vars to the configuration/deploy section if one lists env vars; otherwise skip)
- Test: whole suite

**Interfaces:**
- Consumes: everything above.
- Produces: nothing new.

- [ ] **Step 1: Run the entire default suite**

Run: `uv run pytest`
Expected: PASS (all tests, `@pytest.mark.live` excluded by default). In particular the `sync` engine and end-to-end tests are green untouched, confirming the branch is transparent above the client seam.

- [ ] **Step 2: Full type check and lint**

Run: `uv run mypy --strict src && uv run ruff check . && uv run ruff format --check .`
Expected: no errors

- [ ] **Step 3: Document the new env vars (only if README lists env vars)**

Check whether `README.md` has an env-var / configuration table. Run: `grep -n "PROVGATE_" README.md`. If a config/env section exists, add:

```markdown
- `PROVGATE_INGEST_CHUNK_THRESHOLD_BYTES` — pruned exports at or above this size use the resumable (chunked) upload path instead of a single POST. Default `16777216` (16 MiB); keep it under the Provenance reverse proxy's `client_max_body_size`.
- `PROVGATE_INGEST_CHUNK_SIZE_BYTES` — size of each chunk sent on the resumable path. Default `16777216` (16 MiB); the server clamps to [5 MiB, 512 MiB].
```

If `grep` finds no env-var documentation section, skip this step (do not invent a new section — stay in scope).

- [ ] **Step 4: Commit (only if README changed)**

```bash
git add README.md
git commit -m "docs: document ingest chunk env vars"
```

---

## Self-Review

**Spec coverage:**
- Two config settings with 16 MiB defaults, env-overridable → Task 1. ✓
- Branch on `len(zip_bytes)` vs threshold, single-POST unchanged below → Task 2 (dispatcher + `_ingest_single` preserved byte-for-byte). ✓
- Resumable flow create → parts → complete, server-returned `chunk_size`/`total_parts` honored, 1-indexed parts, byte-exact slicing incl. short final part → Task 2. ✓
- Per-part retry (4 attempts, `500ms·2^n`, injected clock) → Task 2 (loop) + Task 3 (test). ✓
- Best-effort `DELETE` abort on unrecoverable failure, error re-raised, create-failure needs no abort → Task 4. ✓
- Seam unchanged; engine/store/ports/notify untouched; wiring forwards settings → Task 5, regression in Task 6. ✓
- `job_id` from `/complete` flows to `poll_job` (poll_job itself unchanged) → covered by `test_large_payload_is_chunked_and_reassembles` returning the handle; existing `poll_job` tests unchanged. ✓
- Test cases 1–6 from the spec → Tasks 2, 3, 4 map to sub-threshold, chunked-happy, clamp, transient-retry, exhaustion-abort, job_id-flow. ✓
- `mypy --strict` clean, no new `Any`; `ruff` clean → verification steps in every task + Task 6. ✓

**Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". All code shown in full. Task 6 Step 3 is conditional (documented condition + explicit skip), not a placeholder. ✓

**Type consistency:** `_ResumableUpload(upload_id, s3_upload_id, chunk_size, total_parts)` defined in Task 2 and consumed identically in `_ingest_chunked`/`_create_upload`. `_put_part`/`_complete_upload`/`_abort_upload` signatures match their call sites. `ingest_gradescope_export` signature and `-> JobHandle` return unchanged throughout. Constructor kwargs `chunk_threshold_bytes`/`chunk_size_bytes`/`part_max_attempts` named identically in Task 2 definition, Task 3/4 test usage, and Task 5 wiring. ✓

**Note on `_PARTS_RE`:** Defined once in Task 2's test additions and reused by Tasks 3 and 4. Task 5's wiring test inlines its own regex (separate file). Tasks must be implemented in order 1→6 (Task 3/4 tests reference `_PARTS_RE` and `make_client` kwargs introduced in Task 2).
