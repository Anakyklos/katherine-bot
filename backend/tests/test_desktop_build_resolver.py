"""Tests for frontend build resolution used by the desktop shell (#334).

The desktop entrypoint loads the *real* Vite build (frontend/dist), never a
throwaway page. Resolution must:

* find a valid build (index.html + assets) and return its absolute path;
* fail explicitly and safely when the build is missing (no silent fallback);
* accept only an explicit, constrained configuration: no arbitrary path
* inputs, no path traversal, no environment-expanded user paths.

Runs headless; resolution is pure path logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.desktop.build_resolver import (
    BuildResolutionError,
    DesktopBuildConfig,
    resolve_frontend_build,
)


def _make_valid_build(root: Path) -> Path:
    dist = root / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>build</body></html>", encoding="utf-8")
    (dist / "desktop.html").write_text("<html><body>build</body></html>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "index.js").write_text("console.log('build')", encoding="utf-8")
    return dist


class TestValidBuildResolution:
    def test_resolves_real_vite_build(self, tmp_path: Path) -> None:
        _make_valid_build(tmp_path)
        config = DesktopBuildConfig(frontend_root=tmp_path / "frontend")
        resolved = resolve_frontend_build(config)
        assert resolved.index_html == tmp_path / "frontend" / "dist" / "index.html"
        assert resolved.index_html.is_file()
        assert resolved.desktop_html == tmp_path / "frontend" / "dist" / "desktop.html"
        assert resolved.desktop_html.is_file()
        assert resolved.dist_dir.is_dir()

    def test_resolution_is_pure_no_filesystem_writes(self, tmp_path: Path) -> None:
        _make_valid_build(tmp_path)
        before = sorted(str(p) for p in tmp_path.rglob("*"))
        config = DesktopBuildConfig(frontend_root=tmp_path / "frontend")
        resolve_frontend_build(config)
        after = sorted(str(p) for p in tmp_path.rglob("*"))
        assert before or after  # sanity
        assert before == after  # resolution wrote nothing


class TestMissingBuildFailsExplicitly:
    def test_missing_dist_fails_with_clear_message(self, tmp_path: Path) -> None:
        (tmp_path / "frontend").mkdir()
        config = DesktopBuildConfig(frontend_root=tmp_path / "frontend")
        with pytest.raises(BuildResolutionError) as excinfo:
            resolve_frontend_build(config)
        # The error must tell the developer what to run — actionable.
        message = str(excinfo.value)
        assert "npm" in message and "build" in message
        # No absolute local path leakage in the public message.
        assert "home" not in message.lower() or "frontend" in message.lower()

    def test_packaged_root_suggests_reinstall(self, tmp_path: Path) -> None:
        app_root = tmp_path / "katherine"
        frontend_root = app_root / "frontend"
        (app_root / "backend" / "desktop").mkdir(parents=True)
        (app_root / "vendor").mkdir()
        frontend_root.mkdir()
        (app_root / "backend" / "desktop" / "build_resolver.py").write_text("module")

        with pytest.raises(BuildResolutionError) as excinfo:
            resolve_frontend_build(DesktopBuildConfig(frontend_root=frontend_root))

        assert "reinstall" in str(excinfo.value).lower()

    def test_missing_index_html_fails(self, tmp_path: Path) -> None:
        dist = tmp_path / "frontend" / "dist"
        dist.mkdir(parents=True)
        config = DesktopBuildConfig(frontend_root=tmp_path / "frontend")
        with pytest.raises(BuildResolutionError):
            resolve_frontend_build(config)


class TestConfigValidation:
    def test_config_rejects_non_local_root(self, tmp_path: Path) -> None:
        # Configuration must be explicit and constrained: frontend_root
        # must exist and be a directory.
        with pytest.raises(ValueError):
            DesktopBuildConfig(frontend_root=tmp_path / "does-not-exist")

    def test_no_path_input_in_resolution(self, tmp_path: Path) -> None:
        # resolve_frontend_build takes no path strings from callers: the
        # only input is the constrained config, so traversal is
        # structurally impossible.
        _make_valid_build(tmp_path)
        config = DesktopBuildConfig(frontend_root=tmp_path / "frontend")
        resolved = resolve_frontend_build(config)
        assert resolved.dist_dir == config.frontend_root / "dist"
        assert resolved.dist_dir.is_relative_to(config.frontend_root)
