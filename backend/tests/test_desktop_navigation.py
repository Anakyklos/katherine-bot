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
from backend.desktop.api import DESKTOP_API_METHODS, make_js_api
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
        assert wrapper.health() == {"ok": True, "api_version": 2}

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
        # The race found during validation: get_uri() already returned a
        # local file:// URL (revert load started) while the remote document
        # is still alive. The committed URL is the previous local page and
        # the current URL is a DIFFERENT local one, so they disagree — the
        # mismatch alone refuses. (The harder variant — same URL — is
        # covered by TestRevertToSameEntryUrl: URL equality must not be
        # trusted as proof that the local load completed.)
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
        assert sorted(public) == sorted(DESKTOP_API_METHODS)

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
        assert js_api.health() == {"ok": True, "api_version": 2}

    def test_js_api_health_remote_via_wrapper(self, monkeypatch, tmp_path: Path) -> None:
        self._run_with_stubbed_webview(monkeypatch, tmp_path, "https://example.com/")
        _, recorded = self._run_with_stubbed_webview(monkeypatch, tmp_path, "https://example.com/")
        js_api = recorded["create_window_kwargs"]["js_api"]
        assert js_api.health()["code"] == ERROR_BRIDGE_UNAVAILABLE


class TestRevertToSameEntryUrl:
    """The REAL revert race: same entry URL, not a different local one.

    WebKitGTK's ``get_uri()`` reflects the load that *started*. During
    the revert (remote detected → ``load_url(entry_html)``), the URL can
    already read the SAME ``file://`` entry while the **remote document
    is still alive** and the new local load has NOT completed. URL
    equality between ``current_url`` and the previously committed URL
    proves neither document identity nor load completion, so a trust
    model based only on that equality serves the bridge to the remote
    document. These tests model the machine of states directly:

    * entry local committed → navigation to remote detected → trust
      REVOKED (before any revert load_url);
    * revert started, ``get_uri()`` flipped back to the SAME entry URL,
      new local load NOT completed → bridge still refused;
    * new local load completed (loaded event, new commit) → bridge
      serves again.

    Each state is driven through the same functions the real wiring
    uses; nothing here depends on WebKitGTK timing.
    """

    # ---- State machine modeled exactly (same entry URL) --------------

    def test_remote_detected_revokes_trust_before_revert_starts(
        self, tmp_path: Path
    ) -> None:
        # Immediately when a loaded event reports a non-local URL, the
        # trust must be revoked — BEFORE any load_url() navigation back
        # begins (the old handler kept the commit alive across the
        # revert, which is what made the same-URL window dangerous).
        build = _make_build(tmp_path)
        entry_uri = build.index_html.as_uri()
        trust = BuildTrust(build)
        assert trust.commit_if_local(entry_uri) is True

        # Records the trust state at the exact moment the revert
        # navigation is issued (inside load_url).
        trust_at_revert: list[bool] = []

        class _Window(_FakeWindowForPolicy):
            def load_url(self, url: str) -> None:
                trust_at_revert.append(trust.is_trusted(entry_uri))
                super().load_url(url)

        window = _Window(entry_uri)
        window.show("https://example.com/")  # loaded event: remote URL
        machine = desktop_app_module.make_navigation_policy(
            window=window, build=build, entry_uri=entry_uri, trust=trust
        )
        machine.on_loaded()

        # Revoked BEFORE the revert navigation was issued...
        assert trust_at_revert == [False]
        # ...and stays revoked for every URL, including the entry.
        assert trust.is_trusted(entry_uri) is False
        assert trust.is_trusted("https://example.com/") is False
        assert window.load_url_calls == [entry_uri]

    def test_same_url_while_new_local_load_incomplete_keeps_bridge_refused(
        self, tmp_path: Path
    ) -> None:
        # The heart of the race: after the revert load_url() to the SAME
        # entry_html, get_uri() already returns that same file:// URL
        # while the remote document is still alive and the new local
        # load has not completed. A trust model that only compares URLs
        # would reopen the bridge here — health() must still refuse.
        build = _make_build(tmp_path)
        entry_uri = build.index_html.as_uri()
        trust = BuildTrust(build)
        assert trust.commit_if_local(entry_uri) is True

        # The loaded event reports the REMOTE document; the policy
        # revokes and issues the revert.
        window = _FakeWindowForPolicy("https://example.com/")
        machine = desktop_app_module.make_navigation_policy(
            window=window, build=build, entry_uri=entry_uri, trust=trust
        )
        machine.on_loaded()
        assert window.load_url_calls == [entry_uri]

        # Revert started: get_uri() has flipped back to the SAME entry
        # URL while the remote document is still alive and the new
        # local load has NOT fired its loaded event yet.
        window.show(entry_uri)
        assert window.get_current_url() == entry_uri
        bridge = LocalBuildBridge(
            make_js_api(), build, window.get_current_url, trust
        )
        assert bridge.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

        # Only after the new local load completes (loaded event → new
        # commit) may the bridge serve again — same URL, new state.
        machine.on_loaded()
        assert bridge.health() == {"ok": True, "api_version": 2}

    def test_bridge_reopens_only_after_new_local_load_completes(
        self, tmp_path: Path
    ) -> None:
        # Full loop: commit → remote detected (revoke) → revert to the
        # same entry URL (URL already flipped, still refused) → new
        # local load COMPLETES (loaded event) → bridge serves again.
        build = _make_build(tmp_path)
        entry_uri = build.index_html.as_uri()
        trust = BuildTrust(build)
        assert trust.commit_if_local(entry_uri) is True

        window = _FakeWindowForPolicy(entry_uri)
        machine = desktop_app_module.make_navigation_policy(
            window=window, build=build, entry_uri=entry_uri, trust=trust
        )

        # 1) Remote detected: trust revoked before the revert.
        window.show("https://example.com/")
        machine.on_loaded()
        assert trust.is_trusted(entry_uri) is False

        # 2) Revert started: URL flipped back to the same entry while the
        #    remote document is still alive; the bridge keeps refusing.
        window.show(entry_uri)
        bridge = LocalBuildBridge(make_js_api(), build, window.get_current_url, trust)
        assert bridge.health()["code"] == ERROR_BRIDGE_UNAVAILABLE

        # 3) New local load completes: the loaded event re-commits and
        #    the bridge serves again.
        machine.on_loaded()
        assert bridge.health() == {"ok": True, "api_version": 2}

    def test_revert_navigates_to_the_entry_url(self, tmp_path: Path) -> None:
        # The revert load goes back to the same entry_html the shell
        # opened (as_uri()), never anywhere else.
        build = _make_build(tmp_path)
        entry_uri = build.index_html.as_uri()
        window = _FakeWindowForPolicy(entry_uri)
        machine = desktop_app_module.make_navigation_policy(
            window=window, build=build, entry_uri=entry_uri
        )
        window.show("https://example.com/")
        machine.on_loaded()
        assert window.load_url_calls == [entry_uri]

    # ---- Handler wiring (stubbed webview, real run_desktop_shell) ----

    def test_run_desktop_shell_wires_policy_with_revoke(self, monkeypatch, tmp_path: Path) -> None:
        # run_desktop_shell must install a loaded handler that (a) serves
        # the committed local page, (b) revokes the trust the moment a
        # remote load is reported and reverts, (c) keeps the bridge
        # closed while the revert load has flipped get_uri() back to the
        # SAME entry URL but the new local load has not completed, and
        # (d) re-opens only after the new local load completed.
        recorded = _run_shell_with_scripted_urls(monkeypatch, tmp_path)
        # (a) initial local load: bridge serves.
        assert recorded["health_after_first_load"]["ok"] is True
        # (b) remote loaded event: revert issued to the entry URL...
        assert recorded["load_url_calls"] == [
            (tmp_path / "dist" / "index.html").resolve().as_uri()
        ]
        # ...and the bridge refuses the remote document.
        assert (
            recorded["health_after_remote_loaded"]["code"] == ERROR_BRIDGE_UNAVAILABLE
        )
        # (c) mid-revert: SAME entry URL visible, new load incomplete —
        # the bridge must STILL refuse (this is the race being closed).
        assert recorded["health_mid_revert"]["code"] == ERROR_BRIDGE_UNAVAILABLE
        # (d) new local load completed: bridge serves again.
        assert recorded["health_after_reload"]["ok"] is True


class _FakeWindowForPolicy:
    """Minimal window double for the policy machine tests.

    ``show(url)`` scripts what ``get_current_url()`` returns (modeling
    WebKitGTK's get_uri flipping as loads start), and records
    ``load_url`` calls.
    """

    def __init__(self, initial_url: str) -> None:
        self._current = initial_url
        self.load_url_calls: list[str] = []

    def get_current_url(self) -> str | None:
        return self._current

    def show(self, url: str) -> None:
        self._current = url

    def load_url(self, url: str) -> None:
        self.load_url_calls.append(url)


def _run_shell_with_scripted_urls(monkeypatch, tmp_path: Path) -> dict:
    """Drive ``run_desktop_shell`` through the same-URL revert race.

    The stubbed window scripts the exact WebKitGTK behavior of the race:
    (1) initial local load completes; (2) a remote document loads;
    (3) the shell reverts and get_uri() flips to the SAME entry URL
    while the new local load is still incomplete — a bridge call made
    at that moment must be refused; (4) the new local load completes
    and the bridge serves again.

    ``webview.start()`` plays the role of the GTK main loop: it fires
    the registered ``loaded`` handlers at each scripted step (the real
    GTK backend fires them from ``load_changed``/FINISHED).
    """
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    entry_uri = (dist / "index.html").resolve().as_uri()
    remote_uri = "https://example.com/"

    recorded: dict = {
        "health_after_first_load": None,
        "health_mid_revert": None,
        "health_after_remote_loaded": None,
        "health_after_reload": None,
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
        """Scripts get_current_url() as WebKitGTK does during the race.

        State machine: the window shows one document at a time;
        ``load_url`` starts a navigation whose URL is visible
        immediately (get_uri reflects the load that STARTED) but whose
        ``loaded`` event only fires later, when the harness lets the
        main loop run (``finish_loads()``).
        """

        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs
            self.events = FakeEvents()
            self._current: str | None = None
            self._pending_load: str | None = None
            self.js_api = kwargs.get("js_api")

        def get_current_url(self) -> str | None:
            return self._current

        def load_url(self, url: str) -> None:
            recorded["load_url_calls"].append(url)
            self._pending_load = url
            # WebKitGTK: get_uri() flips as soon as the load starts,
            # even though the previous (remote) document is still the
            # live document until the new load completes.
            self._current = url

        def fire_loaded(self) -> None:
            for handler in list(self.events.loaded.handlers):
                handler()

        def finish_loads(self) -> None:
            if self._pending_load is not None:
                self._pending_load = None
                self.fire_loaded()

        def navigate(self, url: str) -> None:
            # A same-window navigation not issued by the shell (e.g. a
            # link click or location assignment in the page).
            self._current = url
            self.fire_loaded()

        def health(self) -> dict:
            return self.js_api.health()

    window_holder: list = []

    def _create_window(**kwargs):
        window = FakeWindow(**kwargs)
        window_holder.append(window)
        return window

    def _start():
        win = window_holder[0]
        # 1) The initial local load completes (loaded fires, commit).
        win._current = entry_uri
        win.fire_loaded()
        recorded["health_after_first_load"] = win.health()
        # 2) Same-window navigation to a remote document; its load
        #    completes (loaded fires with the remote URL) → the shell
        #    must revoke the trust and issue the revert.
        win.navigate(remote_uri)
        recorded["health_after_remote_loaded"] = win.health()
        # 3) Mid-revert: load_url(entry) already flipped get_uri() to
        #    the SAME entry URL while the remote document is still the
        #    live document and the new local load has NOT completed.
        #    A bridge call made right now must be refused.
        recorded["health_mid_revert"] = win.health()
        # 4) The new local load completes (loaded fires) → commit →
        #    the bridge serves the local document again.
        win.finish_loads()
        recorded["health_after_reload"] = win.health()

    fake_webview = types.SimpleNamespace(
        create_window=_create_window,
        start=_start,
    )
    monkeypatch.setattr(desktop_app_module, "webview", fake_webview)

    desktop_app_module.run_desktop_shell(frontend_root=tmp_path)
    return recorded


# =========================================================================
# #336 extension: every companion op fails closed (T007)
# =========================================================================

class TestCompanionOpsFailClosed:
    """The full allowlist (not just health) refuses non-local pages.

    Parameterized over every op so a new allowlist entry cannot ship
    without a fail-closed test of its own.
    """

    # (op name, args to call it with)
    _OPS = [
        ("health", ()),
        ("runtime_state", ()),
        ("load_history", ()),
        ("send_message", ("req-1", "hello")),
        ("delete_history", ()),
        ("delete_memories", ()),
        ("reset_emotional_state", ()),
        ("reset_relationship_state", ()),
    ]

    def _wrapper_for(self, build: ResolvedBuild, url: str | None):
        trust = BuildTrust(build)
        return LocalBuildBridge(make_js_api(runtime=_NoopRuntime()), build, lambda: url, trust)

    def test_every_op_refuses_remote_url(self, tmp_path: Path) -> None:
        import json

        build = _make_build(tmp_path)
        wrapper = self._wrapper_for(build, "https://evil.example/")
        for op, args in self._OPS:
            result = getattr(wrapper, op)(*args)
            assert result["ok"] is False, op
            assert result["code"] == ERROR_BRIDGE_UNAVAILABLE, op
            serialized = json.dumps(result)
            assert "evil" not in serialized, op
            assert "Traceback" not in serialized, op

    def test_every_op_refuses_mid_revert(self, tmp_path: Path) -> None:
        # Committed local page, window navigated remote, then the revert
        # load started (get_uri reads local again) — trust was revoked, so
        # every op must still refuse.
        build = _make_build(tmp_path)
        trust = BuildTrust(build)
        local = build.index_html.as_uri()
        trust.commit_if_local(local)
        trust.revoke()  # the non-local load report dropped the trust
        wrapper = LocalBuildBridge(
            make_js_api(runtime=_NoopRuntime()), build, lambda: local, trust
        )
        for op, args in self._OPS:
            assert getattr(wrapper, op)(*args)["code"] == ERROR_BRIDGE_UNAVAILABLE, op

    def test_every_op_serves_the_trusted_local_page(self, tmp_path: Path) -> None:
        # Happy path: a committed local page is served.
        build = _make_build(tmp_path)
        trust = BuildTrust(build)
        local = build.index_html.as_uri()
        trust.commit_if_local(local)
        wrapper = LocalBuildBridge(
            make_js_api(runtime=_NoopRuntime()), build, lambda: local, trust
        )
        for op, args in self._OPS:
            result = getattr(wrapper, op)(*args)
            assert result.get("ok") is True or result.get("success") is True, op

    def test_every_op_refuses_uncommitted_local_page(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)
        wrapper = self._wrapper_for(build, build.index_html.as_uri())
        for op, args in self._OPS:
            assert getattr(wrapper, op)(*args)["code"] == ERROR_BRIDGE_UNAVAILABLE, op

    def test_url_lookup_failure_fails_closed_for_every_op(self, tmp_path: Path) -> None:
        build = _make_build(tmp_path)

        def _raising() -> str | None:
            raise RuntimeError("boom")

        wrapper = LocalBuildBridge(
            make_js_api(runtime=_NoopRuntime()), build, _raising
        )
        for op, args in self._OPS:
            assert getattr(wrapper, op)(*args)["code"] == ERROR_BRIDGE_UNAVAILABLE, op


class _NoopRuntime:
    """Runtime double whose every method returns an ``ok`` payload."""

    def runtime_state(self) -> dict:
        return {"ok": True, "storage": True, "provider_configured": True, "revision": 0}

    def load_history(self, limit: int = 50) -> list:
        return []

    def send_turn(self, *, request_id: str, message: str) -> dict:
        return {"success": True, "response": "ok"}

    def delete_history(self) -> dict:
        return {"success": True, "result": {"status": "applied"}}

    def delete_memories(self) -> dict:
        return {"success": True, "result": {"status": "applied"}}

    def reset_emotional_state(self) -> dict:
        return {"success": True, "result": {"status": "applied"}}

    def reset_relationship_state(self) -> dict:
        return {"success": True, "result": {"status": "applied"}}
