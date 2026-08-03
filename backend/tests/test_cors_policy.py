"""Tests for the CORS origin allowlist parsing and middleware behavior."""

from __future__ import annotations

import os
import sys

import pytest
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


@pytest.fixture(scope="module")
def cors_client():
    """TestClient with a fresh backend.main import under a test CORS env."""
    _original_modules = dict(sys.modules)
    _original_env = dict(os.environ)
    os.environ.update(
        {
            "GROQ_API_KEY": "mock_groq_key_placeholder",
            "GROQ_API_KEY_2": "mock_groq_key_2_placeholder",
            "ADMISSION_HMAC_SECRET": "test-admission-secret-that-is-at-least-32-bytes",
            "TRUSTED_PROXY_CIDRS": "",
            "CORS_ALLOWED_ORIGINS": "http://allowed.example, http://allowed.example",
        }
    )
    sys.modules.pop("backend.main", None)

    from backend.main import app

    yield TestClient(app)

    os.environ.clear()
    os.environ.update(_original_env)
    if "backend.main" in _original_modules and _original_modules["backend.main"] is not None:
        sys.modules["backend.main"] = _original_modules["backend.main"]
    else:
        sys.modules.pop("backend.main", None)


class TestCorsMiddlewareBehavior:
    def test_allowed_origin_is_reflected(self, cors_client: TestClient):
        response = cors_client.get("/health", headers={"Origin": "http://allowed.example"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://allowed.example"

    def test_disallowed_origin_is_not_allowed(self, cors_client: TestClient):
        response = cors_client.get("/health", headers={"Origin": "http://denied.example"})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
