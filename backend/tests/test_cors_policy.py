"""Tests for the CORS origin allowlist parsing and middleware behavior.

The middleware behavior is exercised against a minimal in-test FastAPI app
so no global state is mutated: ``backend.main`` (engine, managers,
Supabase/SentenceTransformer) is never imported here. The real wiring of
``backend.main`` through the parser is covered by the CI docker job against
the actual stack.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from backend.cors_policy import DEFAULT_ALLOWED_ORIGINS, parse_cors_allowed_origins


class TestParseCorsAllowedOrigins:
    def test_absent_var_preserves_legacy_default(self):
        assert parse_cors_allowed_origins(None) == DEFAULT_ALLOWED_ORIGINS

    def test_single_origin(self):
        assert parse_cors_allowed_origins("http://api.example") == ("http://api.example",)

    def test_entries_are_trimmed(self):
        assert parse_cors_allowed_origins(" http://a.example , http://b.example ") == (
            "http://a.example",
            "http://b.example",
        )

    def test_duplicates_are_deduplicated_keeping_order(self):
        assert parse_cors_allowed_origins("http://a.example,http://b.example,http://a.example") == (
            "http://a.example",
            "http://b.example",
        )

    def test_empty_string_fails(self):
        with pytest.raises(ValueError):
            parse_cors_allowed_origins("")

    def test_whitespace_only_fails(self):
        with pytest.raises(ValueError):
            parse_cors_allowed_origins("   ")

    def test_all_empty_entries_fail(self):
        with pytest.raises(ValueError):
            parse_cors_allowed_origins(" , , ")

    def test_wildcard_fails(self):
        with pytest.raises(ValueError):
            parse_cors_allowed_origins("*")

    def test_wildcard_among_origins_fails(self):
        with pytest.raises(ValueError):
            parse_cors_allowed_origins("http://a.example,*")


def _client_with_origins(raw: str | None) -> TestClient:
    """Minimal app wired exactly like backend.main: parser -> middleware."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(parse_cors_allowed_origins(raw)),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health():
        return {"status": "alive"}

    return TestClient(app)


ALLOWED = "http://allowed.example"
DENIED = "http://denied.example"


class TestCorsMiddlewareBehavior:
    def test_allowed_origin_is_reflected(self):
        response = _client_with_origins(ALLOWED).get(
            "/health", headers={"Origin": ALLOWED}
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ALLOWED

    def test_disallowed_origin_is_not_allowed(self):
        response = _client_with_origins(ALLOWED).get(
            "/health", headers={"Origin": DENIED}
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    def test_allowed_preflight_succeeds(self):
        response = _client_with_origins(ALLOWED).options(
            "/health",
            headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ALLOWED
        assert response.headers["access-control-allow-methods"]

    def test_disallowed_preflight_is_denied(self):
        response = _client_with_origins(ALLOWED).options(
            "/health",
            headers={"Origin": DENIED, "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers
