"""Optional tests. The verifier runs these with the script's own dependencies plus pytest."""

from __future__ import annotations

import httpx
import pytest

from main import fetch_json, is_transient, make_retrying, scripted_transport


def _client(script: list[int | Exception]) -> httpx.Client:
    return httpx.Client(transport=scripted_transport(iter(script)), base_url="https://example.invalid")


@pytest.mark.parametrize(("status", "expected"), [(429, True), (503, True), (400, False), (404, False)])
def test_status_classification(status: int, expected: bool) -> None:
    request = httpx.Request("GET", "https://example.invalid")
    exc = httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    assert is_transient(exc) is expected


def test_invalid_url_is_not_transient() -> None:
    assert is_transient(httpx.InvalidURL("nope")) is False


def test_recovers_after_transient_failures() -> None:
    with _client([503, httpx.ReadTimeout("slow"), 200]) as client:
        assert fetch_json(client, "/x", make_retrying(initial_wait=0.001, max_wait=0.002))["status"] == 200


def test_reraises_original_error_when_exhausted() -> None:
    with _client([503, 503, 503]) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_json(client, "/x", make_retrying(max_attempts=3, initial_wait=0.001, max_wait=0.002))


def test_non_transient_is_single_attempt() -> None:
    # If a 404 were retried, the second response (200) would succeed and nothing would raise.
    with _client([404, 200]) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_json(client, "/x", make_retrying(initial_wait=0.001, max_wait=0.002))
