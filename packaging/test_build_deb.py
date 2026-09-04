"""Unit contracts for the Linux desktop package builder (#338).

These tests do not download dependencies or invoke dpkg. The real
install/build lifecycle is exercised by packaging/smoke_deb.py; this
module protects the small pure helpers and package invariants.
"""

from __future__ import annotations

import importlib.util
import os
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "packaging" / "build_deb.py"
_spec = importlib.util.spec_from_file_location("katherine_build_deb", BUILD_SCRIPT)
assert _spec is not None and _spec.loader is not None
build_deb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_deb)


def test_desktop_file_list_is_explicit_and_exists() -> None:
    assert "backend/__init__.py" not in build_deb.DESKTOP_BACKEND_FILES
    assert "backend/admission_contracts.py" in build_deb.DESKTOP_BACKEND_FILES
    assert all((REPO_ROOT / rel).is_file() for rel in build_deb.DESKTOP_BACKEND_FILES)


def test_desktop_lock_contains_only_pinned_runtime_dependencies() -> None:
    pins = dict(build_deb.parse_lock(REPO_ROOT / "packaging/requirements-desktop.txt"))
    assert pins["pywebview"] == "6.2.1"
    assert pins["proxy-tools"] == "0.1.0"
    assert not any(name.startswith(prefix) for name in pins for prefix in (
        "fastapi",
        "uvicorn",
        "supabase",
        "torch",
        "sentence-transformers",
        "numpy",
    ))


def test_vendor_guard_rejects_cloud_or_ml_stack(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "fastapi").mkdir()
    with pytest.raises(SystemExit):
        build_deb.guard_vendor(vendor)


def test_vendor_guard_accepts_desktop_closure(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "webview").mkdir()
    (vendor / "pydantic").mkdir()
    build_deb.guard_vendor(vendor)


def test_unpack_sdist_extracts_proxy_tools_package(tmp_path: Path) -> None:
    source_root = tmp_path / "proxy_tools-0.1.0"
    package = source_root / "proxy_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source_root / "proxy_tools.egg-info").mkdir()
    archive = tmp_path / "proxy_tools-0.1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source_root, arcname=source_root.name)

    vendor = tmp_path / "vendor"
    vendor.mkdir()
    build_deb.unpack_sdist(archive, vendor)
    assert (vendor / "proxy_tools/__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_entrypoint_derives_app_dir_from_its_own_location() -> None:
    assert "readlink -f \"$0\"" in build_deb.ENTRYPOINT
    assert 'APP=$(dirname "$(dirname "$SELF")")/lib/katherine' in build_deb.ENTRYPOINT
    assert "/usr/lib/katherine" not in build_deb.ENTRYPOINT


def test_normalize_stage_mtimes_makes_archive_inputs_deterministic(tmp_path: Path) -> None:
    """The builder must normalize copied source mtimes before dpkg-deb."""
    nested = tmp_path / "usr/lib/katherine"
    nested.mkdir(parents=True)
    file_path = nested / "module.py"
    file_path.write_text("pass\n", encoding="utf-8")
    old = 1_700_000_000
    os.utime(file_path, (old, old))

    build_deb.normalize_stage_mtimes(tmp_path, 1234567890)

    for path in [tmp_path, nested, file_path]:
        assert path.stat().st_mtime_ns == 1234567890 * 1_000_000_000


def test_normalize_stage_modes_uses_debian_safe_defaults(tmp_path: Path) -> None:
    package_dir = tmp_path / "usr/lib/katherine"
    package_dir.mkdir(parents=True)
    entrypoint = tmp_path / "usr/bin/katherine"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    package_file = package_dir / "module.py"
    package_file.write_text("pass\n", encoding="utf-8")
    package_file.chmod(0o777)

    build_deb.normalize_stage_modes(tmp_path)

    assert package_dir.stat().st_mode & 0o777 == 0o755
    assert package_file.stat().st_mode & 0o777 == 0o644
    assert entrypoint.stat().st_mode & 0o777 == 0o755
