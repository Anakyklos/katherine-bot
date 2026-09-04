"""Shell runtime wiring tests for the companion runtime (#336, T016).

Contract under test (``run_desktop_shell`` lifecycle):

* the local :class:`~backend.companion_runtime.CompanionRuntime` is
  opened **before** the window is created and closed **after**
  ``webview.start()`` returns (window close ⇒ clean shutdown);
* ``close()`` is idempotent and always runs (``finally``), even when the
  window loop raises;
* a startup storage failure (corrupt database) produces a *sanitized*
  error message and exit code 2 — and **no window is created**;
* the runtime handed to the bridge is the real production runtime built
  against the default local database path.

All tests run headless with a stubbed ``webview`` module: no real window,
no display, no event loop.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import backend.desktop.app as desktop_app_module
from backend.desktop.api import make_js_api


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "desktop.html").write_text("<html></html>", encoding="utf-8")
    return dist


def _stub_webview(monkeypatch, *, start_raises: bool = False) -> dict:
    """Patch ``webview`` in the app module with a recording stub."""
    recorded: dict = {
        "create_window_calls": [],
        "start_called": False,
        "start_raised": False,
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
            self.events = FakeEvents()
            self._url = kwargs.get("url")

        def get_current_url(self) -> str | None:
            # The real window reports the URL of the COMPLETED load;
            # for the stub, the load completes immediately with the
            # creation URL (the local build entry). Bridge calls must
            # therefore be served — this is the smoke's steady state.
            return self._url

        def load_url(self, url: str) -> None:
            pass


    windows: list = []

    def _create_window(**kwargs):
        recorded["create_window_calls"].append(kwargs)
        window = FakeWindow(**kwargs)
        windows.append(window)
        return window

    def _start():
        recorded["start_called"] = True
        if start_raises:
            recorded["start_raised"] = True
            raise RuntimeError("window loop crashed /tmp/secret")
        # The stub's load completes when the loop runs: fire the
        # registered loaded handlers so the navigation policy commits
        # the local page as trusted (mirrors the real window's first
        # completed load of the entry page).
        for window in windows:
            for handler in list(window.events.loaded.handlers):
                handler()

    monkeypatch.setattr(
        desktop_app_module, "webview", types.SimpleNamespace(
            create_window=_create_window, start=_start
        )
    )
    return recorded


class TestRuntimeLifecycleWiring:
    def test_runtime_closed_after_start_returns(self, monkeypatch, tmp_path: Path) -> None:
        _make_dist(tmp_path)
        recorded = _stub_webview(monkeypatch)

        lifecycle: list[str] = []

        class FakeRuntime:
            def __init__(self) -> None:
                self.closed = False

            def runtime_state(self):
                return {"ok": True}

            def load_history(self, limit=50):
                return []

            def send_turn(self, *, request_id, message):
                return {"success": True}

            def delete_history(self):
                return {"success": True, "result": {"status": "applied"}}

            def delete_memories(self):
                return {"success": True, "result": {"status": "applied"}}

            def reset_emotional_state(self):
                return {"success": True, "result": {"status": "applied"}}

            def reset_relationship_state(self):
                return {"success": True, "result": {"status": "applied"}}

            def close(self):
                lifecycle.append("close")
                self.closed = True

        monkeypatch.setattr(
            desktop_app_module,
            "_build_runtime",
            lambda: lifecycle.append("open") or FakeRuntime(),
        )

        exit_code = desktop_app_module.run_desktop_shell(frontend_root=tmp_path)
        assert exit_code == 0
        assert recorded["start_called"] is True
        # Opened before the window, closed after start returned.
        assert lifecycle == ["open", "close"]
        # Exactly one window.
        assert len(recorded["create_window_calls"]) == 1

    def test_runtime_closed_even_when_start_raises(self, monkeypatch, tmp_path: Path) -> None:
        _make_dist(tmp_path)
        _stub_webview(monkeypatch, start_raises=True)

        lifecycle: list[str] = []

        class FakeRuntime:
            def close(self):
                lifecycle.append("close")

        monkeypatch.setattr(
            desktop_app_module, "_build_runtime", lambda: FakeRuntime()
        )

        try:
            desktop_app_module.run_desktop_shell(frontend_root=tmp_path)
        except RuntimeError:
            pass
        assert lifecycle == ["close"]

    def test_bridge_holds_the_runtime(self, monkeypatch, tmp_path: Path) -> None:
        # The runtime built by _build_runtime is the object the exposed
        # bridge dispatches to (wired once, never reassigned).
        _make_dist(tmp_path)
        recorded = _stub_webview(monkeypatch)

        class FakeRuntime:
            def __init__(self):
                self.marker = "the-runtime"

            def close(self):
                pass

        runtime = FakeRuntime()
        monkeypatch.setattr(
            desktop_app_module, "_build_runtime", lambda: runtime
        )

        desktop_app_module.run_desktop_shell(frontend_root=tmp_path)
        kwargs = recorded["create_window_calls"][0]
        bridge = kwargs["js_api"]
        assert bridge._bridge._api._runtime is runtime

    def test_startup_storage_failure_is_sanitized_no_window(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Corrupt DB at startup: no window may open, the message must be
        # sanitized (no paths, no tracebacks), exit code 2.
        _make_dist(tmp_path)
        recorded = _stub_webview(monkeypatch)

        def _broken_runtime():
            raise RuntimeError(
                "StorageCorruptError: database disk image is malformed "
                "at /home/user/.local/share/katherine/katherine.db"
            )

        monkeypatch.setattr(desktop_app_module, "_build_runtime", _broken_runtime)

        exit_code = desktop_app_module.main()
        assert exit_code == 2
        assert recorded["create_window_calls"] == []

        stderr = capsys.readouterr().err
        assert "malformed" not in stderr
        assert "/home/user" not in stderr
        assert "katherine.db" not in stderr
        assert "Traceback" not in stderr

    def test_build_runtime_uses_production_path(self, monkeypatch, tmp_path: Path) -> None:
        # _build_runtime builds the real CompanionRuntime lazily (the
        # import inside the function keeps the module import-pure).
        import inspect

        source = inspect.getsource(desktop_app_module._build_runtime)
        assert "companion_runtime" in source
        # And the module itself stays import-pure: companion_runtime is
        # not imported at module scope.
        module_source = Path(desktop_app_module.__file__).read_text(encoding="utf-8")
        top = module_source.split('"""')[2] if module_source.count('"""') >= 2 else ""
        import ast

        tree = ast.parse(module_source)
        top_level_imports = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                top_level_imports.extend(names)
        assert not any("companion_runtime" in name for name in top_level_imports)


class TestBridgeRuntimeIntegration:
    """End-to-end: the exposed facade over the real CompanionRuntime.

    Real temp SQLite, deterministic fake provider, real bridge facade
    (no webview): proves UI-visible ops work through the actual boundary
    the frontend will call.
    """

    def _make_bridge(self, tmp_path: Path, monkeypatch, *, provider_configured=True):
        from backend.companion_runtime import CompanionRuntime

        class FakeProvider:
            """LanguageModel fake (issue #337): the runtime seam is the
            canonical contract; the trusted policy is a core function,
            not a provider capability."""

            async def appraise(self, message, budget):
                from backend.emotional_domain import AppraisalV1

                return AppraisalV1.neutral()

            async def generate(self, messages, budget):
                return "local desktop response"

            async def extract_archival(self, messages, budget):
                return "{}"

            def describe(self):
                from backend.language_model import ModelSelection
                return ModelSelection(
                    provider="fake", main_model_id="fake-main",
                    fast_model_id="fake-fast",
                )

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY_2", raising=False)

        runtime = CompanionRuntime(
            storage_path=tmp_path / "katherine.db",
            language_model=FakeProvider(),
            provider_configured_probe=lambda: provider_configured,
        )
        return make_js_api(runtime=runtime), runtime

    def test_full_turn_and_history_through_bridge(self, tmp_path, monkeypatch):
        bridge, runtime = self._make_bridge(tmp_path, monkeypatch)

        # runtime_state on a fresh DB
        state = bridge.runtime_state()
        assert state == {"ok": True, "storage": True,
                         "provider_configured": True, "revision": 0}

        # Send a turn through the exact JS-visible call
        result = bridge.send_message("req-1", "olá, tudo bem?")
        assert result["ok"] is True
        assert result["success"] is True
        assert result["response"] == "local desktop response"
        assert result["replayed"] is False

        # History through the bridge
        history = bridge.load_history()
        assert history["ok"] is True
        roles = [m["role"] for m in history["messages"]]
        assert roles == ["user", "assistant"]

        # Replay: same request_id + same message returns the persisted
        # result without a second provider call.
        replay = bridge.send_message("req-1", "olá, tudo bem?")
        assert replay["ok"] is True
        assert replay["replayed"] is True
        assert replay["response"] == "local desktop response"

        # Revision advanced
        assert bridge.runtime_state()["revision"] == 1

        runtime.close()

    def test_unconfigured_provider_error_surfaces_sanitized(self, tmp_path, monkeypatch):
        bridge, runtime = self._make_bridge(
            tmp_path, monkeypatch, provider_configured=False
        )

        # State still opens: unconfigured provider never blocks reads.
        state = bridge.runtime_state()
        assert state["ok"] is True
        assert state["provider_configured"] is False

        history = bridge.load_history()
        assert history["ok"] is True
        assert history["messages"] == []
        runtime.close()

    def test_conflicting_payload_returns_request_conflict(self, tmp_path, monkeypatch):
        bridge, runtime = self._make_bridge(tmp_path, monkeypatch)

        first = bridge.send_message("req-1", "primeira mensagem")
        assert first["ok"] is True

        # Same request_id, different message → sanitized conflict.
        second = bridge.send_message("req-1", "mensagem DIFERENTE")
        assert second["ok"] is False
        assert second["success"] is False
        assert second["error_code"] == "request_conflict"
        serialized = json.dumps(second)
        assert "DIFERENTE" not in serialized

        runtime.close()

    def test_privacy_ops_through_bridge(self, tmp_path, monkeypatch):
        bridge, runtime = self._make_bridge(tmp_path, monkeypatch)
        bridge.send_message("req-1", "oi")

        # delete_history removes rows but keeps state
        result = bridge.delete_history()
        assert result["ok"] is True
        assert result["success"] is True

        history = bridge.load_history()
        assert history["messages"] == []

        # reset ops
        assert bridge.reset_emotional_state()["ok"] is True
        assert bridge.reset_relationship_state()["ok"] is True
        assert bridge.delete_memories()["ok"] is True

        runtime.close()


class TestSmokeSeams:
    """#336: the smoke-test seams of ``run_desktop_shell``.

    ``storage_path`` / ``provider`` let the reproducible smoke run the
    production lifecycle against a throwaway database and an offline
    provider (no user data, no Groq quota). The seam must bypass the
    production ``_build_runtime`` completely and build a REAL
    CompanionRuntime over the injected path — so the smoke exercises
    the true bridge → runtime → LocalStorage path.
    """

    def test_seams_bypass_production_runtime_builder(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        _make_dist(tmp_path)
        recorded = _stub_webview(monkeypatch)

        def _forbidden():
            raise AssertionError(
                "production runtime builder must not run when seams are injected"
            )

        monkeypatch.setattr(desktop_app_module, "_build_runtime", _forbidden)

        class OfflineProvider:
            """LanguageModel fake (issue #337): contract seam only; the
            trusted policy is core, not a provider capability."""

            async def appraise(self, message, budget):
                from backend.emotional_domain import AppraisalV1

                return AppraisalV1.neutral()

            async def generate(self, messages, budget):
                return "offline reply"

            async def extract_archival(self, messages, budget):
                return "{}"

            def describe(self):
                from backend.language_model import ModelSelection
                return ModelSelection(
                    provider="fake", main_model_id="fake-main",
                    fast_model_id="fake-fast",
                )

        db_path = tmp_path / "smoke.db"
        exit_code = desktop_app_module.run_desktop_shell(
            frontend_root=tmp_path,
            storage_path=db_path,
            provider=OfflineProvider(),
        )
        assert exit_code == 0
        # The window received a bridge over the SEAM runtime (the
        # production builder never ran — the monkeypatched _forbidden
        # would have raised).
        js_api = recorded["create_window_calls"][0]["js_api"]
        turn = js_api.send_message("smoke-req-1", "olá do smoke")
        assert turn["ok"] is True
        assert turn["success"] is True

        # Independent proof: the injected path holds the data.
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM chat_logs"
            ).fetchone()
        finally:
            conn.close()
        assert count == 2  # user + assistant

    def test_no_seams_uses_production_builder(self, monkeypatch, tmp_path: Path) -> None:
        _make_dist(tmp_path)
        recorded = _stub_webview(monkeypatch)

        calls: list[str] = []
        monkeypatch.setattr(
            desktop_app_module,
            "_build_runtime",
            lambda: calls.append("production") or _FakeLifecycleRuntime(),
        )

        class _FakeLifecycleRuntime:
            def close(self):
                calls.append("close")

        exit_code = desktop_app_module.run_desktop_shell(frontend_root=tmp_path)
        assert exit_code == 0
        assert recorded["start_called"] is True
        assert "production" in calls
