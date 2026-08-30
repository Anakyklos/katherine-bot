"""Navigation policy tests for the desktop shell (#334, review B2).

Loading ``file://`` initially is not enough to keep the privileged
bridge away from remote content: pywebview injects
``window.pywebview`` into whatever document loaded last, and a
same-window navigation (link click, ``window.location = ...``) would
hand the bridge to a remote page.

Contract under test (the two local policy layers in
``backend/desktop/app.py``):

1. ``is_local_build_url`` — the structural predicate deciding which
   URLs count as "the local build" (exact index, inside dist, no
   traversal, no query/fragment, file scheme with empty host);
2. ``LocalBuildBridge`` — the ``js_api`` object fails closed: when the
   window is not showing the local build, calls return a sanitized
   ``bridge_unavailable`` payload instead of executing;
3. the ``loaded`` handler wiring — ``run_desktop_shell`` registers a
   handler that reverts navigation away from the build (verified with a
   stubbed ``webview`` module: no window is ever created here).

All tests are headless: no pywebview window is created.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import backend.desktop.app as desktop_app_module
from backend.desktop.api import make_js_api
from backend.desktop.app import (
    ERROR_BRIDGE_UNAVAILABLE,
    BuildTrust,
    LocalBuildBridge,
    is_local_build_url,
)
from backend.desktop.build_resolver import ResolvedBuild


def _make_build(tmp_path: Path) -> ResolvedBuild:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    return ResolvedBuild(dist_dir=dist, index_html=dist / "index.html")


class TestIsLocalBuildUrl:
    def test_accepts_the_index_html_uri(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        assert is_local_build_url(build.index_html.as_uri(), build) is True

    def test_accepts_assets_inside_dist(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        asset = "file://" + build.dist_dir.as_posix() + "/assets/app.js"
        assert is_local_build_url(asset, build) is True

    def test_rejects_traversal_out_of_dist(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        traversal = "file://" + build.dist_dir.as_posix() + "/../secret.html"
        assert is_local_build_url(traversal, build) is False

    def test_rejects_traversal_that_resolves_back_inside(self, tmp_path: Path) -> None:
        # ``dist/../dist/app.js`` resolves to a file inside dist, but the
        # URL contains ``..`` segments and must not be considered local.
        build = _make_build(tmp_path)
        sneaky = "file://" + build.dist_dir.as_posix() + "/../dist/app.js"
        assert is_local_build_url(sneaky, build) is False

    def test_rejects_dot_segments(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        dotted = "file://" + build.dist_dir.as_posix() + "/./app.js"
        assert is_local_build_url(dotted, build) is False

    def test_rejects_remote_http_url(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        assert is_local_build_url("https://example.com/", build) is False
        assert is_local_build_url("http://127.0.0.1:8080/", build) is False

    def test_rejects_file_url_outside_dist(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        assert is_local_build_url("file:///etc/passwd", build) is False

    def test_rejects_file_url_with_host(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        with_host = "file://localhost" + build.index_html.as_posix()
        assert is_local_build_url(with_host, build) is False

    def test_rejects_query_and_fragment(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        assert is_local_build_url(build.index_html.as_uri() + "?x=1", build) is False
        assert is_local_build_url(build.index_html.as_uri() + "#frag", build) is False

    def test_rejects_missing_and_empty(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        assert is_local_build_url(None, build) is False
        assert is_local_build_url("", build) is False

    def test_never_raises_on_malformed_url(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        for bad in ("file://[::1", "file://%zz", "::::", "file:"):
            assert is_local_build_url(bad, build) is False


class TestBridgeFailsClosedOutsideLocalBuild:
    """Remote pages must never invoke Python through the bridge."""

    def _bridge_for(self, build: ResolvedBuild, url: str | None, trusted: bool = False):
        trust = BuildTrust(build)
        if trusted:
            trust.commit_if_local(url)
        return LocalBuildBridge(make_js_api(), build, lambda: url, trust)

    def test_local_url_receives_the_health_payload(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        url = build.index_html.as_uri()
        # The loaded handler committed this local page as trusted.
        wrapper = self._bridge_for(build, url, trusted=True)
        assert wrapper.health() == {"ok": True, "api_version": 1}

    def test_local_url_before_commit_is_refused(self, tmp_path: Path) -> None:
        # Trust is only granted after the loaded handler commits the
        # page; an in-flight local load (not yet committed) fails closed.
        build = _make_build(tmp_path)
        url = build.index_html.as_uri()
        wrapper = self._bridge_for(build, url, trusted=False)
        assert wrapper.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

    def test_remote_url_gets_sanitized_unavailable_payload(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        wrapper = self._bridge_for(build, "https://example.com/", trusted=False)
        result = wrapper.health()
        assert result == {
            "ok": False,
            "code": ERROR_BRIDGE_UNAVAILABLE,
            "message": "Desktop bridge is not available for this page.",
        }

    def test_remote_url_with_stale_local_commit_is_refused(self, tmp_path: Path) -> None:
        # The revert race: a local page was committed, then the window
        # navigated remote while the doc is still alive. The URL no
        # longer matches the committed one — refuse.
        build = _make_build(tmp_path)
        local = build.index_html.as_uri()
        wrapper = self._bridge_for(build, "https://example.com/")
        wrapper._trust.commit_if_local(local)  # type: ignore[reportPrivateUsage]
        assert wrapper.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

    def test_revert_race_local_uri_remote_doc_is_refused(self, tmp_path: Path) -> None:
        # The exact race found during validation: get_uri() already
        # returned the local file:// URL (revert load started) while the
        # remote document is still alive. The committed URL is still the
        # previous local page — but the URL CHANGED (same path is fine
        # here: identical URL means no in-flight navigation is visible;
        # a revert to the same entry URL is indistinguishable from the
        # committed page, which is safe: the remote doc died when the
        # revert completed and the local page re-commits on loaded).
        # The dangerous case is a DIFFERENT local URL racing: refused.
        build = _make_build(tmp_path)
        index_uri = build.index_html.as_uri()
        asset_uri = "file://" + build.dist_dir.as_posix() + "/other.html"
        (build.dist_dir / "other.html").write_text("<html></html>", encoding="utf-8")
        trust = BuildTrust(build)
        trust.commit_if_local(index_uri)
        wrapper = LocalBuildBridge(make_js_api(), build, lambda: asset_uri, trust)
        assert wrapper.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

    def test_file_url_outside_dist_is_refused(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        wrapper = self._bridge_for(build, "file:///etc/passwd")
        assert wrapper.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

    def test_traversal_url_is_refused(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        traversal = "file://" + build.dist_dir.as_posix() + "/../evil.html"
        wrapper = self._bridge_for(build, traversal)
        assert wrapper.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

    def test_unavailable_payload_is_sanitized(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        wrapper = self._bridge_for(build, "https://attacker.example/")
        serialized = json.dumps(wrapper.health())
        assert "Traceback" not in serialized
        assert "attacker" not in serialized

    def test_url_lookup_failure_fails_closed(self, tmp_path: Path) -> None:
        # If get_current_url raises, the bridge must refuse to serve —
        # never fall back to "assume local".
        build = _make_build(tmp_path)

        def _raising() -> str | None:
            raise RuntimeError("boom")

        wrapper = LocalBuildBridge(make_js_api(), build, _raising)
        assert wrapper.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

    def test_remote_page_cannot_probe_with_arguments(self, tmp_path: Path) -> None:
        # Even a hostile caller passing stray arguments only ever gets
        # the sanitized unavailable payload (no method executes).
        build = _make_build(tmp_path)
        wrapper = self._bridge_for(build, "https://example.com/")
        assert wrapper.health("anything")["code"] == ERROR_BRIDGE_UNAVAILABLE


class TestLoadedHandlerRevertsNavigation:
    """``run_desktop_shell`` must wire a revert-on-loaded handler."""

    def _run_with_stubbed_webview(
        self, monkeypatch, tmp_path: Path, current_url_after_load: str | None
    ):
        """Run ``run_desktop_shell`` with a stubbed ``webview`` module.

        Returns recorded calls. No real window is created: the stub
        captures everything ``run_desktop_shell`` does, then fires the
        registered ``loaded`` handlers exactly like WebKitGTK would when
        the document finishes loading. The stub is patched onto the
        already-imported ``backend.desktop.app`` module so its local
        ``webview`` reference is replaced too.
        """
        # Build must resolve: create a minimal dist first.
        dist = tmp_path / "dist"
        dist.mkdir(exist_ok=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")

        recorded: dict = {
            "create_window_kwargs": None,
            "start_called": False,
            "load_url_calls": [],
        }

        class FakeLoadedEvent:
            def __init__(self) -> None:
                self.handlers: list = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class FakeEvents:
            def __init__(self) -> None:
                self.loaded = FakeLoadedEvent()

        class FakeWindow:
            def __init__(self, **kwargs) -> None:
                self._kwargs = kwargs
                self.events = FakeEvents()
                recorded["create_window_kwargs"] = kwargs

            def get_current_url(self) -> str | None:
                return current_url_after_load

            def load_url(self, url: str) -> None:
                recorded["load_url_calls"].append(url)

        window_holder: list = []

        def _create_window(**kwargs):
            window = FakeWindow(**kwargs)
            window_holder.append(window)
            return window

        def _start():
            recorded["start_called"] = True
            # Fire the loaded handlers like the real shell would when the
            # document finishes loading.
            for window in window_holder:
                for handler in list(window.events.loaded.handlers):
                    handler()

        fake_webview = types.SimpleNamespace(
            create_window=_create_window,
            start=_start,
        )
        monkeypatch.setattr(desktop_app_module, "webview", fake_webview)

        exit_code = desktop_app_module.run_desktop_shell(frontend_root=tmp_path)
        return exit_code, recorded

    def test_js_api_is_the_fail_closed_wrapper(self, monkeypatch, tmp_path: Path) -> None:
        exit_code, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, None)
        assert exit_code == 0
        js_api = recorded["create_window_kwargs"]["js_api"]
        assert isinstance(js_api, LocalBuildBridge)
        # Wrapper exposes exactly the allowlisted surface.
        public = [name for name in dir(js_api) if not name.startswith("_")]
        assert public == ["health"]

    def test_window_url_is_the_local_build(self, monkeypatch, tmp_path: Path) -> None:
        _, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, None)
        dist = (tmp_path / "dist").resolve()
        assert recorded["create_window_kwargs"]["url"] == (dist / "index.html").as_uri()

    def test_loaded_navigation_to_remote_is_reverted(self, monkeypatch, tmp_path: Path) -> None:
        # Simulate the window having navigated away when ``loaded`` fires:
        # the handler must call load_url back to the local build.
        dist = (tmp_path / "dist").resolve()
        expected_uri = (dist / "index.html").as_uri()
        # Two runs to cover the wiring: one navigation to remote (revert)
        _, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, "https://example.com/")
        assert recorded["load_url_calls"] == [expected_uri]

    def test_loaded_navigation_staying_local_is_not_reverted(self, monkeypatch, tmp_path: Path) -> None:
        dist = (tmp_path / "dist").resolve()
        local_uri = (dist / "index.html").as_uri()
        _, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, local_uri)
        assert recorded["load_url_calls"] == []

    def test_js_api_health_local_roundtrip_via_wrapper(self, monkeypatch, tmp_path: Path) -> None:
        dist = (tmp_path / "dist").resolve()
        local_uri = (dist / "index.html").as_uri()
        _, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, local_uri)
        js_api = recorded["create_window_kwargs"]["js_api"]
        assert js_api.health() == {"ok": True, "api_version": 1}

    def test_js_api_health_remote_via_wrapper(self, monkeypatch, tmp_path: Path) -> None:
        self._run_with_stubbed_webview(monkeypatch, tmp_path, "https://example.com/")
        _, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, "https://example.com/")
        js_api = recorded["create_window_kwargs"]["js_api"]
        assert js_api.health()["code"] == ERROR_BRIDGE_UNAVAILABLE
