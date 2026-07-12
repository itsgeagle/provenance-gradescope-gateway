import httpx
import respx

from provgate.gradescope.client import GradescopeClient

GS = "https://www.gradescope.com"


def make_client() -> GradescopeClient:
    return GradescopeClient(httpx.Client(follow_redirects=True), base_url=GS)


@respx.mock
def test_login_posts_csrf_and_credentials() -> None:
    respx.get(f"{GS}/login").mock(
        return_value=httpx.Response(
            200,
            text='<input name="authenticity_token" value="TOK-1" />',
        )
    )
    login = respx.post(f"{GS}/login").mock(
        return_value=httpx.Response(302, headers={"location": "/account"})
    )
    # httpx.Client(follow_redirects=True) follows the 302 itself, so the landing
    # page must be mocked too or respx raises AllMockedAssertionError instead of
    # hitting the network.
    respx.get(f"{GS}/account").mock(return_value=httpx.Response(200, text="Account"))
    make_client().login("staff@example.edu", "pw")
    body = login.calls.last.request.content.decode()
    assert "TOK-1" in body
    assert "staff%40example.edu" in body  # url-encoded email


@respx.mock
def test_download_export_returns_bytes() -> None:
    url = f"{GS}/courses/1/assignments/2/export/without_evaluations"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"PK\x03\x04zip"))
    data = make_client().download_export("1", "2")
    assert data == b"PK\x03\x04zip"
