import asyncio

import httpx
import pytest

from app.routers import coach


@pytest.mark.parametrize("method,expected_calls", [("GET", 3), ("POST", 1)])
def test_only_read_requests_are_retried_after_uncertain_response(monkeypatch, method, expected_calls):
    calls = []

    async def fail(_self, verb, url, **kwargs):
        calls.append(verb)
        raise httpx.ReadTimeout("timed out waiting for a response")

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(httpx.AsyncClient, "request", fail)
    monkeypatch.setattr(coach.asyncio, "sleep", no_sleep)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(coach._request_with_retry(
            method=method, url="http://agent/coach/action", timeout=1, attempts=3,
        ))
    assert len(calls) == expected_calls
