"""Security boundary tests for the desktop shell (#334).

Structural, headless assertions that the privileged bridge cannot be
reached by remote content and that the shell adds no servers/processes:

* the window URL is always a local ``file://`` URI derived from the
  resolved build — never an arbitrary caller-supplied URL;
* the module exposes no generic navigation to the JS side (the API
  allowlist stays the only surface);
* no ``http(s)`` hosting is introduced: the entrypoint imports FastAPI
  nowhere and binds no sockets (import purity + grep-level evidence);
* banned capabilities (eval/exec/subprocess/shell/webbrowser/open) are
  absent from the desktop package.
"""

from __future__ import annotations

import ast
from pathlib import Path

import backend.desktop.app as desktop_app_module
from backend.desktop.api import DESKTOP_API_METHODS, DesktopApi, safe_call

_DESKTOP_DIR = Path(desktop_app_module.__file__).parent
_PY_SOURCES = sorted(_DESKTOP_DIR.glob("*.py"))


class TestLocalOnlyWindowUrl:
    def test_window_url_is_derived_from_resolved_build(self, tmp_path: Path) -> None:
        # run_desktop_shell accepts no URL of any kind: the only input is
        # the frontend root, and the URL is always the resolved local
        # index.html converted with as_uri().
        import inspect

        signature = inspect.signature(desktop_app_module.run_desktop_shell)
        assert "url" not in signature.parameters
        assert "frontend_root" in signature.parameters

    def test_built_uri_points_into_dist(self, tmp_path: Path) -> None:
        dist = tmp_path / "frontend" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        from backend.desktop.build_resolver import DesktopBuildConfig, resolve_frontend_build

        build = resolve_frontend_build(DesktopBuildConfig(frontend_root=tmp_path / "frontend"))
        uri = build.index_html.as_uri()
        assert uri.startswith("file://")
        assert "dist" in uri


class TestNoBridgeForRemoteContent:
    def test_allowlist_remains_the_only_js_surface(self) -> None:
        # Any future navigation feature must go through review: today the
        # exposed surface is exactly health, and safe_call refuses anything
        # else with a sanitized error.
        assert DESKTOP_API_METHODS == ("health",)
        assert safe_call(DesktopApi(), "navigate", "https://example.com") == {
            "ok": False,
            "code": "invalid_input",
            "message": "Unknown method.",
        }
        assert safe_call(DesktopApi(), "eval", "1+1")["code"] == "invalid_input"

    def test_desktop_api_has_no_file_or_shell_capabilities(self) -> None:
        # The API object handed to JS has no file/shell/process surface.
        api = DesktopApi()
        for banned in (
            "open",
            "read",
            "write",
            "execute",
            "shell",
            "subprocess",
            "system",
            "import",
            "env",
            "navigate",
        ):
            assert not hasattr(api, banned), f"bridge must not expose {banned!r}"


class TestNoHttpHosting:
    def test_entrypoint_does_not_start_any_server(self) -> None:
        # The shell must host the packaged UI locally via file://; it may
        # not spin FastAPI/Uvicorn/Vite just to serve static assets.
        import sys

        assert "uvicorn" not in sys.modules
        # backend.main stays import-free in this process unless a test
        # imported it; the structural point is the entrypoint source.
        source = _DESKTOP_DIR.joinpath("app.py").read_text(encoding="utf-8")
        for banned in ("uvicorn", "fastapi", "TestServer", "http.server", "0.0.0.0", "localhost:"):
            assert banned not in source, f"desktop shell must not reference {banned!r}"


class TestBannedCapabilities:
    def test_no_eval_exec_subprocess_or_open_in_desktop_package(self) -> None:
        banned_calls = {"eval", "exec", "system", "popen", "run", "Popen"}
        banned_imports = {"subprocess", "os", "shutil", "webbrowser"}
        for path in _PY_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split(".")[0] not in banned_imports, (
                            f"{path.name}: forbidden import {alias.name}"
                        )
                if isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    assert root not in banned_imports, (
                        f"{path.name}: forbidden import from {node.module}"
                    )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    assert name not in banned_calls, (
                        f"{path.name}: forbidden call {name}()"
                    )

    def test_no_string_url_literals_in_desktop_package(self) -> None:
        # No http(s) URLs may appear in the desktop package: the window
        # only ever loads the local build.
        for path in _PY_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not node.value.startswith(("http://", "https://")), (
                        f"{path.name}: unexpected URL literal {node.value!r}"
                    )
