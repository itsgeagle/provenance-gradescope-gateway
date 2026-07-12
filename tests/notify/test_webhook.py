import httpx
import respx

from provgate.notify.webhook import post_summary

URL = "https://hooks.example.com/wh"


@respx.mock
def test_post_success_returns_true_and_sends_content() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ok = post_summary(URL, "hello", timeout_s=5.0, http=httpx.Client())
    assert ok is True
    assert (
        route.calls.last.request.content == b'{"content": "hello"}'
        or b'"content"' in route.calls.last.request.content
    )


@respx.mock
def test_non_2xx_returns_false_and_does_not_raise() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500))
    assert post_summary(URL, "x", timeout_s=5.0, http=httpx.Client()) is False


@respx.mock
def test_transport_error_returns_false_and_does_not_raise() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
    assert post_summary(URL, "x", timeout_s=5.0, http=httpx.Client()) is False
