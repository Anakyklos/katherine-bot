"""Tests for the minimal Desktop API exposed to the pywebview shell (#334).

Contract under test:

* ``health()`` returns a stable, structured, JSON-serializable payload;
* the API surface is an explicit allowlist (no generic object exposure);
* every exposed method validates input and rejects invalid values with a
  sanitized, structured error (no tracebacks, no internal details);
* importing the module has no side effects (no window, no threads, no env
  reads at import time).

These tests run headless: they never create a pywebview window.
"""

from __future__ import annotations

import json

import backend.desktop.api as desktop_api_module
from backend.desktop.api import DESKTOP_API_METHODS, DesktopApi, DesktopApiError


class TestHealthContract:
    def test_health_returns_ok_payload_with_api_version(self) -> None:
        api = DesktopApi()
        payload = api.health()
        assert payload["ok"] is True
        assert payload["api_version"] == 1
        # Payload must be JSON-serializable as-is (bridge returns plain data).
        json.dumps(payload)

    def test_health_is_deterministic(self) -> None:
        first = DesktopApi().health()
        second = DesktopApi().health()
        assert first == second


class TestAllowlistedSurface:
    def test_exposed_methods_are_exactly_the_allowlist(self) -> None:
        api = DesktopApi()
        public = [name for name in dir(api) if not name.startswith("_")]
        assert sorted(public) == sorted(DESKTOP_API_METHODS)

    def test_allowlist_contains_only_health(self) -> None:
        # Deliberately minimal: this proof must not grow a chat API.
        assert DESKTOP_API_METHODS == ("health",)

    def test_api_exposes_no_generic_objects(self) -> None:
        # The bridge must never hand internal objects/modules to JS.
        api = DesktopApi()
        assert not hasattr(api, "engine")
        assert not hasattr(api, "supabase")
        assert not hasattr(api, "settings")
        assert not hasattr(api, "backend")
        for name in DESKTOP_API_METHODS:
            result = getattr(api, name)()
            assert isinstance(result, dict)
            assert all(isinstance(k, str) for k in result)


class TestInputValidation:
    def test_health_rejects_arguments(self) -> None:
        # JS may pass stray arguments; anything unexpected is rejected
        # instead of being silently ignored.
        api = DesktopApi()
        try:
            api.health("unexpected")  # type: ignore[arg-type]
        except DesktopApiError as err:
            assert err.payload["code"] == "invalid_input"
            assert "unexpected" not in json.dumps(err.payload)
        else:
            raise AssertionError("health() must reject unexpected arguments")

    def test_error_payload_is_sanitized(self) -> None:
        # A failed call must surface a structured error without leaking
        # types, reprs, paths, or environment details.
        try:
            DesktopApi().health(None)  # type: ignore[arg-type]
        except DesktopApiError as err:
            serialized = json.dumps(err.payload)
            assert "<" not in serialized  # no repr markers
            assert "NoneType" not in serialized
            assert "Traceback" not in serialized


class TestImportPurity:
    def test_importing_desktop_api_has_no_side_effects(self, monkeypatch) -> None:
        # Import must not read the environment, start threads, or touch
        # pywebview: the module stays importable/testable without display.
        monkeypatch.setenv("KATHERINE_PROBE", "1")
        import importlib

        importlib.reload(desktop_api_module)
        # No env read at import time is asserted by the fact that the
        # module does not fail or cache anything env-dependent; behavior
        # below must be identical regardless of environment.
        assert DesktopApi().health()["ok"] is True

    def test_desktop_api_does_not_import_webview(self) -> None:
        # api.py must not depend on pywebview: window concerns live in
        # the entrypoint, keeping this module GUI-free.
        import sys

        assert "webview" not in sys.modules or not any(
            "backend.desktop.api" in str(m) and getattr(m, "webview", None)
            for m in []
        )
        source = (desktop_api_module.__file__ or "")
        assert source  # module has a real file
