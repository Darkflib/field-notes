# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "tenacity>=9.0",
# ]
# ///
"""Retry transient HTTP failures with tenacity, and fail fast on the rest.

Uses httpx.MockTransport so it runs offline and deterministically:
    uv run main.py
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator

import httpx
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger("retry-demo")

# Status codes worth another go. 429 and 503 are the server telling you to back off;
# 502/504 are usually a proxy losing a race. Everything else 4xx is your bug, not theirs.
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """Decide whether an exception is worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    # Connect/read timeouts and dropped connections, but not e.g. InvalidURL.
    return isinstance(exc, httpx.TransportError)


def make_retrying(max_attempts: int = 4, initial_wait: float = 0.5, max_wait: float = 8.0) -> Retrying:
    """Build a retry policy. Parameterised so tests and demos can shrink the waits."""
    return Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_attempts),
        # Jitter matters: without it, every client that failed together retries together.
        wait=wait_exponential_jitter(initial=initial_wait, max=max_wait),
        before_sleep=before_sleep_log(log, logging.WARNING),
        # Re-raise the original exception, not tenacity.RetryError wrapping it.
        reraise=True,
    )


def fetch_json(client: httpx.Client, url: str, retrying: Retrying) -> dict[str, object]:
    """GET a URL and return its JSON body, retrying transient failures.

    Only use this for idempotent requests. Retrying a POST that timed out after the
    server committed it is how you charge someone twice.
    """
    for attempt in retrying:
        with attempt:
            response = client.get(url)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                # Not transient, so this escapes the retry loop immediately.
                raise TypeError(f"expected a JSON object, got {type(body).__name__}")
            return body
    # Unreachable with reraise=True, but mypy can't know that.
    raise AssertionError("retry loop exited without result or exception")


def scripted_transport(responses: Iterator[int | Exception]) -> httpx.MockTransport:
    """A transport that replays a fixed sequence of statuses or exceptions."""

    def handler(request: httpx.Request) -> httpx.Response:
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={"status": item, "path": request.url.path})

    return httpx.MockTransport(handler)


def run_case(name: str, script: list[int | Exception], retrying_factory: Callable[[], Retrying]) -> bool:
    """Run one scenario and report whether it succeeded."""
    transport = scripted_transport(iter(script))
    with httpx.Client(transport=transport, base_url="https://example.invalid") as client:
        try:
            body = fetch_json(client, "/thing", retrying_factory())
        except httpx.HTTPError as exc:
            log.info("%s: gave up with %s: %s", name, type(exc).__name__, exc)
            return False
        log.info("%s: succeeded with %s", name, body)
        return True


def main() -> int:
    """Show retry-then-succeed, fail-fast, and give-up behaviour."""

    # Tiny waits so the demo finishes in well under a second.
    def fast() -> Retrying:
        return make_retrying(initial_wait=0.01, max_wait=0.05)

    outcomes = {
        # Two transient failures, then success.
        "flaky": run_case("flaky", [503, httpx.ConnectError("reset"), 200], fast),
        # 404 is not transient: one attempt, no sleeps.
        "not-found": run_case("not-found", [404, 200], fast),
        # Transient every time: exhausts attempts and re-raises the last error.
        "down": run_case("down", [503, 503, 503, 503, 200], fast),
    }
    expected = {"flaky": True, "not-found": False, "down": False}
    if outcomes != expected:
        log.error("unexpected outcomes: %s (expected %s)", outcomes, expected)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
