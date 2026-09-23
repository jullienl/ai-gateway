"""Tests for the request body-size guard and error-detail truncation.

Importing `app` only defines routes/middleware at module scope; it does not
start the Copilot runtime (that only happens inside the FastAPI `lifespan`,
which these tests never enter), so no live credentials are needed here.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import BodySizeLimitMiddleware, MAX_BODY_BYTES, _MAX_ERROR_DETAIL, _truncated


def _tiny_app() -> FastAPI:
    """A minimal app carrying only the middleware under test, no lifespan."""
    tiny = FastAPI()
    tiny.add_middleware(BodySizeLimitMiddleware)

    @tiny.post("/echo")
    def echo(body: dict):
        return body

    return tiny


class TestBodySizeLimit:
    def test_request_under_the_limit_is_accepted(self):
        client = TestClient(_tiny_app())
        r = client.post("/echo", json={"a": "b"})
        assert r.status_code == 200

    def test_request_over_the_limit_is_rejected(self):
        client = TestClient(_tiny_app())
        oversized = "x" * (MAX_BODY_BYTES + 1)
        r = client.post(
            "/echo",
            content=oversized.encode(),
            headers={"content-length": str(len(oversized)), "content-type": "application/json"},
        )
        assert r.status_code == 413


class TestErrorDetailTruncation:
    def test_short_message_is_returned_unchanged(self):
        assert _truncated(RuntimeError("boom")) == "boom"

    def test_long_message_is_truncated(self):
        long_message = "x" * (_MAX_ERROR_DETAIL + 100)
        result = _truncated(RuntimeError(long_message))
        assert len(result) == _MAX_ERROR_DETAIL
