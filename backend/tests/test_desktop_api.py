"""Tests for the sanitized desktop bridge delivered to pywebview (#334).

Contract under test (review blocker B1): the object *actually handed to
pywebview's ``js_api``* sanitizes every call — the tests exercise the
real boundary object (``make_js_api()`` / ``LocalBuildBridge``), not an
auxiliary helper disconnected from the exposed path.

* ``health()`` returns a stable, structured, JSON-serializable payload;
* the exposed surface is an explicit allowlist and matches exactly what
  pywebview's ``get_functions`` walk would expose (no extra attributes);
* no exposed method ever raises: pywebview 6.2.1 converts uncaught
  ``js_api`` exceptions into a JS ``Error`` carrying ``message``/``name``
  /``stack``; the bridge makes that path unreachable, returning
  sanitized structured payloads instead;
* invalid input is rejected with sanitized errors (no tracebacks, no
  internal details), both on the facade and through the full
  navigation-policy wrapper;
* importing the module has no side effects (no window, no threads, no
  env reads at import time).

These tests run headless: they never create a pywebview window.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import backend.desktop.api as desktop_api_module
from backend.desktop.api import (
    DESKTOP_API_METHODS,
    DESKTOP_API_VERSION,
    DesktopApi,
    DesktopApiError,
    make_js_api,
)
from backend.desktop.app import LocalBuildBridge, is_local_build_url
from backend.desktop.build_resolver import ResolvedBuild


def _make_build(tmp_path: Path) -> ResolvedBuild:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    return ResolvedBuild(dist_dir=dist, index_html=dist / "index.html")


def _pywebview_exposed_names(obj: object) -> set[str]:
    """Mirror pywebview's ``get_functions`` public-attribute walk."""
    return {name for name in dir(obj) if not name.startswith("_")}


class TestHealthContract:
    def test_health_returns_ok_payload_with_api_version(self) -> None:
        bridge = make_js_api()
        payload = bridge.health()
        assert payload == {"ok": True, "api_version": DESKTOP_API_VERSION}
        # Payload must be JSON-serializable as-is (bridge returns plain data).
        json.dumps(payload)

    def test_health_is_deterministic(self) -> None:
        assert make_js_api().health() == make_js_api().health()


class TestRealBoundarySanitization:
    """The object given to ``js_api`` must sanitize, never raise."""

    def test_exposed_methods_are_exactly_the_allowlist(self) -> None:
        # pywebview exposes every public attribute; the facade must keep
        # the exposed surface equal to the allowlist and nothing else.
        bridge = make_js_api()
        assert _pywebview_exposed_names(bridge) == set(DESKTOP_API_METHODS)

    def test_local_build_bridge_exposes_only_health(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        url = build.index_html.as_uri()
        wrapper = LocalBuildBridge(make_js_api(), build, lambda: url)
        assert _pywebview_exposed_names(wrapper) == {"health"}

    def test_bridge_health_never_raises_on_invalid_input(self) -> None:
        # pywebview would convert an exception into a JS Error with a
        # stacktrace; the facade returns data instead.
        bridge = make_js_api()
        result = bridge.health("unexpected")  # type: ignore[arg-type]
        assert result == {
            "ok": False,
            "code": "invalid_input",
            "message": "health() takes no arguments.",
        }

    def test_invalid_input_payload_is_sanitized(self) -> None:
        result = make_js_api().health(None)  # type: ignore[arg-type]
        serialized = json.dumps(result)
        assert "NoneType" not in serialized
        assert "Traceback" not in serialized
        assert "<" not in serialized  # no repr markers

    def test_implementation_errors_collapse_to_sanitized_internal_error(
        self, tmp_path: Path
    ) -> None:
        # Simulate an unexpected bug inside the implementation: the
        # facade must still return data, never raise (pywebview would
        # leak a JS Error with stacktrace otherwise).
        build = _make_build(tmp_path)

        class FakeBridge:
            def health(self, *args: object) -> dict[str, object]:
                raise RuntimeError("secret internal detail /path/to/x")

        from backend.desktop.app import BuildTrust

        trust = BuildTrust(build)
        trust.commit_if_local(build.index_html.as_uri())
        wrapper = LocalBuildBridge(
            FakeBridge(), build, lambda: build.index_html.as_uri(), trust
        )
        result = wrapper.health()
        assert result["ok"] is False
        assert result["code"] == "internal_error"
        serialized = json.dumps(result)
        assert "secret" not in serialized
        assert "/path/to/x" not in serialized
        assert "Traceback" not in serialized
        assert "RuntimeError" not in serialized

    def test_allowlist_contains_only_health(self) -> None:
        # Deliberately minimal: this proof must not grow a chat API.
        assert DESKTOP_API_METHODS == ("health",)

    def test_api_exposes_no_generic_objects(self) -> None:
        # The bridge must never hand internal objects/modules to JS.
        bridge = make_js_api()
        for banned in ("engine", "supabase", "settings", "backend", "api"):
            assert not hasattr(bridge, banned), f"bridge must not expose {banned!r}"
        result = bridge.health()
        assert isinstance(result, dict)
        assert all(isinstance(k, str) for k in result)

    def test_make_js_api_returns_fresh_object_each_call(self) -> None:
        assert make_js_api() is not make_js_api()


class TestDesktopApiImplementation:
    """The implementation class may raise; the facade converts to data."""

    def test_implementation_health_rejects_arguments(self) -> None:
        api = DesktopApi()
        try:
            api.health("unexpected")  # type: ignore[arg-type]
        except DesktopApiError as err:
            assert err.payload["code"] == "invalid_input"
            assert "unexpected" not in json.dumps(err.payload)
        else:
            raise AssertionError("health() must reject unexpected arguments")

    def test_implementation_error_payload_is_sanitized(self) -> None:
        try:
            DesktopApi().health(None)  # type: ignore[arg-type]
        except DesktopApiError as err:
            serialized = json.dumps(err.payload)
            assert "<" not in serialized
            assert "NoneType" not in serialized
            assert "Traceback" not in serialized


class TestImportPurity:
    def test_importing_desktop_api_has_no_side_effects(self, monkeypatch) -> None:
        # Import must not read the environment, start threads, or touch
        # pywebview: the module stays importable/testable without display.
        monkeypatch.setenv("KATHERINE_PROBE", "1")
        import importlib

        importlib.reload(desktop_api_module)
        assert make_js_api().health()["ok"] is True

    def test_desktop_api_does_not_import_webview(self) -> None:
        # api.py must not depend on pywebview: window concerns live in
        # the entrypoint, keeping this module GUI-free.
        source = Path(desktop_api_module.__file__).read_text(encoding="utf-8")
        assert "import webview" not in source
        assert "from webview" not in source

    def test_health_signature_takes_no_named_parameters(self) -> None:
        # pywebview inspects parameter names to build the JS proxy;
        # *args-only keeps the surface stable and simple.
        bridge = make_js_api()
        params = inspect.signature(bridge.health).parameters
        assert all(p.kind is p.VAR_POSITIONAL for p in params.values())
