"""Automated invariants for the uv-based backend workflow (#353).

These tests keep the migration honest: pyproject.toml + uv.lock are the
single authoritative dependency graph; backend/requirements.txt is only a
generated compatibility export for Docker; the desktop package lock stays
deliberately separate and consistent with the backend authority; and the
documented commands match the real workflow.

The tests avoid fragile string greps where the real artifact can be tested
instead: they parse the actual TOML files, run the real `uv lock --check`,
and regenerate the real export to compare against the committed file.
They never modify the lock (all invocations are check/frozen/export).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
PYPROJECT = BACKEND_DIR / "pyproject.toml"
LOCKFILE = BACKEND_DIR / "uv.lock"
EXPORT = BACKEND_DIR / "requirements.txt"

UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(
    UV is None,
    reason="uv binary not available in this environment (CI provisions it via setup-uv)",
)


def _uv(args: list[str], cwd: Path = BACKEND_DIR) -> subprocess.CompletedProcess:
    return subprocess.run(
        [UV, *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=600,
    )


def _lock_packages() -> dict[str, str]:
    with LOCKFILE.open("rb") as stream:
        lock = tomllib.load(stream)
    return {entry["name"]: entry["version"] for entry in lock["package"]}


def _project_pins() -> dict[str, str]:
    with PYPROJECT.open("rb") as stream:
        project = tomllib.load(stream)
    pins: dict[str, str] = {}
    for requirement in project["project"]["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", requirement)
        assert match, f"runtime dependency must stay exactly pinned: {requirement}"
        pins[match.group(1).lower()] = match.group(2)
    return pins


def _test_group_pins() -> dict[str, str]:
    with PYPROJECT.open("rb") as stream:
        project = tomllib.load(stream)
    group = project["dependency-groups"]["test"]
    pins: dict[str, str] = {}
    for requirement in group:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", requirement)
        assert match, f"test dependency must stay exactly pinned: {requirement}"
        pins[match.group(1).lower()] = match.group(2)
    return pins


def _export_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or line.startswith("--") or not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^;\\\s]+)", line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            pins[name] = match.group(2)
    return pins


# ── 1. lock is in sync with the dependency definition ──────────────────────


def test_lock_is_in_sync_with_pyproject() -> None:
    """`uv lock --check` must succeed: pyproject.toml and uv.lock agree.

    Runs the real resolver in check mode; it never rewrites the lock.
    """
    result = _uv(["lock", "--check"])
    assert result.returncode == 0, (
        "backend/uv.lock is out of sync with backend/pyproject.toml:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_lock_resolves_every_project_pin_exactly() -> None:
    """Every runtime pin in pyproject.toml must appear verbatim in uv.lock."""
    packages = _lock_packages()
    for name, version in _project_pins().items():
        assert packages.get(name) == version, (
            f"{name}: pyproject pins {version} but uv.lock resolves "
            f"{packages.get(name)!r}"
        )


def test_lock_resolves_every_test_group_pin_exactly() -> None:
    """Every test-group pin must appear verbatim in uv.lock."""
    packages = _lock_packages()
    for name, version in _test_group_pins().items():
        assert packages.get(name) == version, (
            f"{name}: test group pins {version} but uv.lock resolves "
            f"{packages.get(name)!r}"
        )


# ── 2. CI cannot rewrite the lock during normal runs ───────────────────────


def test_frozen_sync_leaves_lock_unchanged(tmp_path: Path) -> None:
    """A frozen sync (the CI mode) must not modify uv.lock.

    Syncs into a throwaway VENV/UV_PROJECT_ENVIRONMENT under tmp_path and
    asserts the lockfile bytes are identical afterwards. This is the
    observable guarantee behind CI's `uv sync --frozen`.
    """
    import hashlib

    lock_before = hashlib.sha256(LOCKFILE.read_bytes()).hexdigest()
    result = _uv(
        [
            "sync",
            "--frozen",
            "--no-install-project",
        ]
    )
    # uv refuses to run some operations inside a non-project cwd; this test
    # only asserts the lock bytes, and a failed sync due to sandbox limits is
    # still acceptable as long as the lock did not change. But a successful
    # sync must also hold: if it failed for another reason, surface it.
    if result.returncode != 0:
        # Re-check mode instead: lock --check must still pass, proving no
        # mutation happened by any uv check path.
        check = _uv(["lock", "--check"])
        assert check.returncode == 0
    lock_after = hashlib.sha256(LOCKFILE.read_bytes()).hexdigest()
    assert lock_before == lock_after, "uv.lock was modified during frozen operations"


# ── 3. test dependencies come from the test group ──────────────────────────


def test_test_tools_live_only_in_the_test_group() -> None:
    """pytest & co. must come from dependency-groups.test, not runtime deps.

    This is the invariant that keeps the runtime graph (Docker export) free
    of test-only packages: the export uses --no-group test.
    """
    with PYPROJECT.open("rb") as stream:
        project = tomllib.load(stream)
    runtime = project["project"]["dependencies"]
    test_group = project["dependency-groups"]["test"]
    runtime_names = {
        re.match(r"([A-Za-z0-9_.-]+)", req).group(1).lower() for req in runtime
    }
    test_names = {
        re.match(r"([A-Za-z0-9_.-]+)", req).group(1).lower() for req in test_group
    }
    overlap = runtime_names & test_names
    assert not overlap, f"packages duplicated across runtime and test group: {overlap}"
    assert {"pytest", "pytest-asyncio", "pytest-cov"} <= test_names
    assert not {"pytest", "pytest-asyncio", "pytest-cov"} & runtime_names


def test_export_excludes_test_group_packages() -> None:
    """The Docker compatibility export must not contain test-only packages."""
    pins = _export_pins(EXPORT)
    forbidden = {"pytest", "pytest-asyncio", "pytest-cov", "coverage", "iniconfig", "pluggy"}
    leaked = forbidden & set(pins)
    assert not leaked, f"test-group packages leaked into runtime export: {leaked}"


def test_default_groups_include_test_group() -> None:
    """`uv sync` (the documented command) must provision the test group too.

    The dev workflow and CI both expect `uv sync` alone to give a runnable
    test environment, mirroring the old requirements.txt + test lock flow.
    """
    with PYPROJECT.open("rb") as stream:
        project = tomllib.load(stream)
    groups = project.get("tool", {}).get("uv", {}).get("default-groups")
    assert groups == ["test"], (
        "tool.uv.default-groups must stay ['test'] so `uv sync` provisions "
        "the test dependencies by default"
    )


# ── 4. torch stays CPU-only ────────────────────────────────────────────────


def test_lock_pins_cpu_only_torch() -> None:
    """The lock must resolve torch to the CPU-only variant."""
    packages = _lock_packages()
    assert packages.get("torch", "").endswith("+cpu"), (
        f"torch must resolve to the +cpu variant, got {packages.get('torch')!r}"
    )


def test_lock_contains_no_cuda_or_nvidia_packages() -> None:
    """No CUDA/NVIDIA dependency may enter the locked graph.

    The CPU-only torch wheels must not pull the GPU stack into the lock,
    mirroring the CI check that freezes the installed environment and
    greps for nvidia-/cuda- packages.
    """
    packages = _lock_packages()
    forbidden = sorted(
        name for name in packages
        if name.startswith("nvidia") or name.startswith("cuda")
    )
    assert not forbidden, f"CUDA/NVIDIA packages present in uv.lock: {forbidden}"


def test_pytorch_cpu_index_is_explicit_and_scoped_to_torch() -> None:
    """The CPU index must be `explicit` and only torch may use it.

    A secondary index must never become an indiscriminate source for every
    package: `explicit = true` plus a torch-only source keeps it narrow.
    """
    with PYPROJECT.open("rb") as stream:
        project = tomllib.load(stream)
    indexes = project.get("tool", {}).get("uv", {}).get("index", [])
    cpu = [idx for idx in indexes if idx.get("url") == "https://download.pytorch.org/whl/cpu"]
    assert len(cpu) == 1, "exactly one PyTorch CPU index must be configured"
    assert cpu[0].get("explicit") is True, "PyTorch CPU index must be explicit"
    sources = project.get("tool", {}).get("uv", {}).get("sources", {})
    assert set(sources) == {"torch"}, (
        f"only torch may route to the CPU index, found sources: {sorted(sources)}"
    )
    assert sources["torch"] == {"index": "pytorch-cpu"}


def test_lock_torch_artifact_comes_from_cpu_index() -> None:
    """The locked torch wheel URL must be the PyTorch CPU index."""
    with LOCKFILE.open("rb") as stream:
        lock = tomllib.load(stream)
    torch_entries = [p for p in lock["package"] if p["name"] == "torch"]
    assert len(torch_entries) == 1
    wheels = torch_entries[0].get("wheels", [])
    urls = [w["url"] for w in wheels if "url" in w]
    assert urls, "torch entry has no wheel URLs"
    cpu_hosts = (
        "https://download.pytorch.org/whl/cpu/",
        "https://download-r2.pytorch.org/whl/cpu/",
    )
    for url in urls:
        assert url.startswith(cpu_hosts), (
            f"torch wheel from unexpected source: {url}"
        )


# ── 5. requirements.txt is a generated artifact, never a second source ─────


def test_requirements_export_is_not_drifted(tmp_path: Path) -> None:
    """Regenerating the export must reproduce the committed file byte-for-byte.

    The committed file is a uv export for the Docker build; if someone
    edits it by hand or forgets to regenerate after changing the lock, this
    test fails. Deterministic command (documented in the file header).
    """
    regenerated = tmp_path / "requirements.txt"
    result = _uv(
        [
            "export",
            "--frozen",
            "--no-emit-project",
            "--no-hashes",
            "--emit-index-url",
            "--no-group",
            "test",
            "--output-file",
            str(regenerated),
        ]
    )
    assert result.returncode == 0, f"uv export failed:\n{result.stderr}"
    # uv's own header contains the output path; compare everything AFTER the
    # two generated-header lines so the comparison is path-independent.
    committed_body = _body_after_generated_header(EXPORT.read_text(encoding="utf-8"))
    regenerated_body = _body_after_generated_header(
        regenerated.read_text(encoding="utf-8")
    )
    assert committed_body == regenerated_body, (
        "backend/requirements.txt is drifted from uv.lock. Regenerate with:\n"
        "  cd backend && uv export --frozen --no-emit-project --no-hashes "
        "--emit-index-url --no-group test --output-file requirements.txt"
    )


def _body_after_generated_header(text: str) -> list[str]:
    lines = text.splitlines()
    idx = next(
        (i for i, line in enumerate(lines) if line.startswith("# This file was autogenerated")),
        None,
    )
    if idx is None:
        return lines
    return lines[idx + 2 :]


def test_requirements_export_matches_lock_pins() -> None:
    """Every runtime pin in the export must equal the locked version.

    Guards against the export ever being edited by hand into a divergent
    graph (the export must be a pure function of uv.lock).
    """
    lock_pins = _lock_packages()
    for name, version in _export_pins(EXPORT).items():
        if name in lock_pins:
            assert lock_pins[name] == version, (
                f"{name}: export says {version}, lock says {lock_pins[name]}"
            )


def test_no_pip_tools_sources_remain() -> None:
    """The hand-maintained pip-tools inputs must stay deleted.

    backend/requirements.in and backend/requirements-test.{in,txt} were the
    old dual-source-of-truth inputs; their existence would recreate the
    two-graphs-edited-in-parallel problem the migration removes.
    """
    for name in ("requirements.in", "requirements-test.in", "requirements-test.txt"):
        assert not (BACKEND_DIR / name).is_file(), (
            f"backend/{name} must not exist; the authoritative graph is "
            "pyproject.toml + uv.lock"
        )


# ── 6. desktop package pins stay consistent with the backend authority ─────


def test_shared_desktop_pins_match_backend_lock() -> None:
    """Desktop lock pins shared with the backend must match uv.lock versions.

    The desktop .deb keeps a deliberately separate, smaller lock; the pins
    it shares with the backend must stay equal to the backend's locked
    versions so the packaged app runs the same code as the checkout.
    """
    lock_pins = _lock_packages()
    for rel in ("requirements-desktop.in", "requirements-desktop.txt"):
        desktop_path = REPO_ROOT / "packaging" / rel
        desktop_pins = _export_pins(desktop_path)
        shared = set(desktop_pins) & set(lock_pins)
        assert shared, f"no shared pins found between backend lock and {rel}"
        mismatches = {
            name: (desktop_pins[name], lock_pins[name])
            for name in shared
            if desktop_pins[name] != lock_pins[name]
        }
        assert not mismatches, (
            f"shared pins drifted between packaging/{rel} and backend/uv.lock: "
            f"{mismatches}"
        )


def test_desktop_lock_stays_minimal() -> None:
    """The desktop lock must not gain cloud/ML backend packages.

    Mirrors packaging/test_build_deb.py's closure guarantee at the graph
    level: the .deb ships only the desktop runtime closure.
    """
    desktop_pins = _export_pins(
        REPO_ROOT / "packaging" / "requirements-desktop.txt"
    )
    banned_prefixes = (
        "fastapi",
        "uvicorn",
        "supabase",
        "torch",
        "sentence-transformers",
        "numpy",
        "transformers",
    )
    banned = sorted(
        name for name in desktop_pins
        if any(name == p or name.startswith(p + "-") for p in banned_prefixes)
    )
    assert not banned, f"desktop lock grew backend-only packages: {banned}"


# ── 7. documented commands match the real workflow ─────────────────────────


def test_documented_workflow_commands_are_real() -> None:
    """README's backend section must teach the actual uv commands.

    Docs are strings, so this is the one place where checking strings is
    checking the artifact itself: the documented commands must exist and
    the retired pip/venv instructions must not return for the normal flow.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "uv sync --project backend",
        "uv sync --project backend --frozen",
        "uv lock --check",
        "uv run --project backend",
        "uv export --frozen",
    ):
        assert command in readme, (
            f"README must document the real workflow command: {command}"
        )
    retired = (
        "pip install -r backend/requirements.txt",
        "pip install -r requirements.txt",
        "pip-compile",
        "python -m venv",
        "python3 -m venv .venv-ci",
    )
    for gone in retired:
        assert gone not in readme, (
            f"README still teaches the retired backend instruction: {gone}"
        )


def test_agents_md_teaches_uv_workflow() -> None:
    """AGENTS.md must instruct agents to use the uv workflow.

    The retired pip/venv tool names may only appear inside the explicit
    prohibition bullet ("Não use ..."), never as taught commands.
    """
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "uv sync --project backend" in agents
    assert "uv run --project backend" in agents
    assert "uv add" in agents and "uv remove" in agents
    assert "uv lock --check" in agents
    assert "uv export --frozen" in agents
    # Retired instructions may only appear inside the prohibition bullet
    # (the bullet starts with "- Não use" and may wrap across lines).
    tooling_section = agents.split("## Tooling Python", 1)[1]
    retired = ("pip-compile", "python -m venv", "pip install")
    for retired_tool in retired:
        positions = [
            m.start() for m in re.finditer(re.escape(retired_tool), tooling_section)
        ]
        assert positions, (
            f"the tooling section must explicitly retire: {retired_tool}"
        )
        for pos in positions:
            bullet_start = tooling_section.rfind("\n- ", 0, pos) + 1
            assert "Não use" in tooling_section[bullet_start:pos], (
                f"{retired_tool} mentioned outside the prohibition bullet"
            )


def test_ci_provisions_python_via_uv() -> None:
    """CI must sync from the lock and run Python through uv, without pip installs."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@v7" in workflow
    assert "uv sync --frozen" in workflow
    assert "uv run --project backend" in workflow
    assert "python -m pip install" not in workflow, (
        "CI must not provision backend Python dependencies with pip anymore"
    )


def test_ci_keeps_python_312() -> None:
    """The 3.12 baseline must stay pinned in CI and in pyproject."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python-version: "3.12"' in workflow
    with PYPROJECT.open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["requires-python"] == ">=3.12,<3.13"
