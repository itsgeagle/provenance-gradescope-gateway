import re
from pathlib import Path

import httpx
import respx

from provgate.cli.wiring import real_gs_login, real_prov
from provgate.config import Settings

BASE = "https://prov.example.edu/api/v1"
GS = "https://www.gradescope.com"


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


@respx.mock
def test_real_gs_login_builds_client_with_poll_settings() -> None:
    # Mock the login round-trip so the factory's login() succeeds without network.
    respx.get(f"{GS}/login").mock(
        return_value=httpx.Response(200, text='<input name="authenticity_token" value="T" />')
    )
    respx.post(f"{GS}/login").mock(
        return_value=httpx.Response(302, headers={"location": "/account"})
    )
    respx.get(f"{GS}/account").mock(return_value=httpx.Response(200, text="ok"))

    settings = Settings(
        db_path=Path("/tmp/x.db"),
        secret_key="k",
        gs_export_poll_interval_s=1.5,
        gs_export_poll_timeout_s=90.0,
    )
    client = real_gs_login(settings)("staff@x.edu", "pw")
    # The constructed client carries the poll settings from config (white-box wiring check).
    assert client._poll_interval_s == 1.5
    assert client._poll_timeout_s == 90.0
    client.close()
