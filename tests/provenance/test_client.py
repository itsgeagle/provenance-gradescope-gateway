import httpx
import pytest
import respx

from provgate.provenance.client import ProvenanceClient, ProvenanceError

BASE = "https://prov.example.edu/api/v1"


def make_client() -> ProvenanceClient:
    # sleep is a no-op; monotonic advances so timeout logic is deterministic
    ticks = iter(range(0, 10_000, 1))
    return ProvenanceClient(
        httpx.Client(),
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
    )


@respx.mock
def test_ingest_returns_job_handle() -> None:
    route = respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "job-9"})
    )
    handle = make_client().ingest_gradescope_export(BASE, "tok", "sem-1", b"zip-bytes")
    assert handle.job_id == "job-9"
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_ingest_non_202_raises() -> None:
    respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    with pytest.raises(ProvenanceError):
        make_client().ingest_gradescope_export(BASE, "tok", "sem-1", b"z")


@respx.mock
def test_poll_until_terminal() -> None:
    url = f"{BASE}/semesters/sem-1/ingest/jobs/job-9"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json={"status": "running"}),
            httpx.Response(200, json={"status": "running"}),
            httpx.Response(200, json={"status": "succeeded"}),
        ]
    )
    status = make_client().poll_job(BASE, "tok", "sem-1", "job-9")
    assert status.status == "succeeded"
    assert status.is_success


@respx.mock
def test_poll_times_out() -> None:
    url = f"{BASE}/semesters/sem-1/ingest/jobs/job-9"
    respx.get(url).mock(return_value=httpx.Response(200, json={"status": "running"}))
    with pytest.raises(ProvenanceError):
        make_client().poll_job(BASE, "tok", "sem-1", "job-9")


@respx.mock
def test_poll_returns_failed_status() -> None:
    url = f"{BASE}/semesters/sem-1/ingest/jobs/job-9"
    respx.get(url).mock(return_value=httpx.Response(200, json={"status": "failed"}))
    status = make_client().poll_job(BASE, "tok", "sem-1", "job-9")
    assert status.status == "failed"
    assert status.is_success is False


@respx.mock
def test_verify_token_true_on_200() -> None:
    route = respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(200, json={"email": "a@b.edu"})
    )
    assert make_client().verify_token(BASE, "tok") is True
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_verify_token_false_on_401() -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(401))
    assert make_client().verify_token(BASE, "tok") is False


@respx.mock
def test_verify_token_false_on_403() -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(403))
    assert make_client().verify_token(BASE, "tok") is False


@respx.mock
def test_verify_token_raises_on_other_error() -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(500))
    with pytest.raises(ProvenanceError):
        make_client().verify_token(BASE, "tok")


@respx.mock
def test_ingest_non_json_body_raises_provenance_error() -> None:
    respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(
            202, content=b"not json", headers={"content-type": "text/plain"}
        )
    )
    with pytest.raises(ProvenanceError):
        make_client().ingest_gradescope_export(BASE, "tok", "sem-1", b"z")
