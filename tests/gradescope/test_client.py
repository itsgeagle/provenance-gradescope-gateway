import tempfile
from pathlib import Path

import httpx
import pytest
import respx

from provgate.gradescope.client import GradescopeClient, GradescopeError

GS = "https://www.gradescope.com"
S3 = "https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/uploads/generated_file/file/42/submissions.zip?sig=x"


def make_client(**kw: object) -> GradescopeClient:
    ticks = iter(range(0, 100_000))
    return GradescopeClient(
        httpx.Client(follow_redirects=True),
        base_url=GS,
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
        **kw,  # type: ignore[arg-type]
    )


@respx.mock
def test_login_posts_csrf_and_credentials() -> None:
    respx.get(f"{GS}/login").mock(
        return_value=httpx.Response(200, text='<input name="authenticity_token" value="TOK-1" />')
    )
    login = respx.post(f"{GS}/login").mock(
        return_value=httpx.Response(302, headers={"location": "/account"})
    )
    respx.get(f"{GS}/account").mock(return_value=httpx.Response(200, text="Account"))
    make_client().login("staff@example.edu", "pw")
    assert "TOK-1" in login.calls.last.request.content.decode()


def _mock_assignment_page() -> None:
    respx.get(f"{GS}/courses/1/assignments/2").mock(
        return_value=httpx.Response(200, text='<meta name="csrf-token" content="CSRF-1" />')
    )


@respx.mock
def test_download_export_create_poll_download() -> None:
    _mock_assignment_page()
    create = respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        side_effect=[
            httpx.Response(200, json={"progress": 0.0, "status": "processing", "url": S3}),
            httpx.Response(200, json={"progress": 1.0, "status": "completed", "url": S3}),
        ]
    )
    respx.get(url=S3).mock(return_value=httpx.Response(200, content=b"PK-zip-bytes"))

    with make_client().download_export("1", "2") as path:
        assert isinstance(path, Path)
        assert path.read_bytes() == b"PK-zip-bytes"
        held = path
    # deleted on context exit
    assert not held.exists()
    # create POST carried the CSRF header
    assert create.calls.last.request.headers["x-csrf-token"] == "CSRF-1"


@respx.mock
def test_download_export_ignores_url_until_completed() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    # url present while still processing must NOT trigger download
    status = respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        side_effect=[
            httpx.Response(200, json={"progress": 0.5, "status": "processing", "url": S3}),
            httpx.Response(200, json={"progress": 1.0, "status": "completed", "url": S3}),
        ]
    )
    dl = respx.get(url=S3).mock(return_value=httpx.Response(200, content=b"z"))
    with make_client().download_export("1", "2"):
        pass
    assert status.call_count == 2  # polled twice
    assert dl.call_count == 1  # downloaded once, after completed


@respx.mock
def test_download_export_failed_status_raises() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        return_value=httpx.Response(200, json={"progress": 0.0, "status": "failed", "url": None})
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_download_export_poll_timeout_raises() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        return_value=httpx.Response(200, json={"progress": 0.1, "status": "processing", "url": S3})
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_download_export_missing_csrf_raises() -> None:
    respx.get(f"{GS}/courses/1/assignments/2").mock(
        return_value=httpx.Response(200, text="<html>no meta</html>")
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_list_assignments_wraps_parse_error_as_gradescope_error() -> None:
    respx.get(f"{GS}/courses/1/assignments").mock(
        return_value=httpx.Response(200, text="<html>no react table</html>")
    )
    with pytest.raises(GradescopeError):
        make_client().list_assignments("1")


@respx.mock
def test_create_export_non_200_raises() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(return_value=httpx.Response(500))
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_create_export_non_numeric_id_raises() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": "not-a-number"})
    )
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass


@respx.mock
def test_download_non_200_raises_and_leaves_no_temp_file(tmp_path: object) -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        return_value=httpx.Response(200, json={"progress": 1.0, "status": "completed", "url": S3})
    )
    respx.get(url=S3).mock(return_value=httpx.Response(404))
    before = set(Path(tempfile.gettempdir()).glob("provgate-export-*.zip"))
    with pytest.raises(GradescopeError):
        with make_client().download_export("1", "2"):
            pass
    after = set(Path(tempfile.gettempdir()).glob("provgate-export-*.zip"))
    assert before == after  # no leaked temp file


@respx.mock
def test_download_reassembles_multiple_chunks() -> None:
    _mock_assignment_page()
    respx.post(f"{GS}/courses/1/assignments/2/export").mock(
        return_value=httpx.Response(200, json={"generated_file_id": 42})
    )
    respx.get(f"{GS}/courses/1/generated_files/42.json").mock(
        return_value=httpx.Response(200, json={"progress": 1.0, "status": "completed", "url": S3})
    )
    respx.get(url=S3).mock(
        return_value=httpx.Response(200, stream=httpx.ByteStream(b"aaaa" + b"bbbb"))
    )
    with make_client().download_export("1", "2") as path:
        assert path.read_bytes() == b"aaaabbbb"
