"""Import-graph isolation proofs for the desktop runtime (#336).

The companion desktop runtime must open, load history, commit turns and
serve privacy ops **without** the cloud stack ever being imported.
This is issue #336's "no Supabase at runtime" requirement made
mechanical: a fresh interpreter imports the runtime graph (module import
plus a first-use factory call) and we assert on ``sys.modules``.

Why a subprocess: the main test process legitimately imports the web
stack elsewhere in the suite, so in-process assertions would prove
nothing. The child process is clean by construction and returns its
``sys.modules`` key set as JSON.

The child never touches the network and never opens a real window: the
provider factory is only *imported* (lazy import is the code path under
test), no client is constructed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD_SCRIPT = r"""
import json
import sys

# Fresh interpreter: importing the desktop graph is the act under test.
import backend.companion_runtime  # noqa: F401
import backend.desktop.api  # noqa: F401
import backend.desktop.app  # noqa: F401

# First-use lazy path: the provider factory's own lazy import target
# (module only, no client construction, no network).
from backend.companion_runtime import build_groq_runtime_provider  # noqa: F401

print(json.dumps(sorted(sys.modules.keys())))
"""


def _child_module_keys() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, (
        "desktop graph import failed in a clean interpreter:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    last_line = result.stdout.strip().splitlines()[-1]
    return set(json.loads(last_line))


def test_desktop_graph_never_imports_supabase() -> None:
    """No supabase module of any flavor enters the desktop graph."""
    keys = _child_module_keys()
    offenders = sorted(k for k in keys if k == "supabase" or k.startswith("supabase."))
    assert offenders == [], (
        "the desktop runtime graph imported Supabase modules: "
        f"{offenders}. The companion desktop path must be Supabase-free "
        "(no auth, no database, no secrets)."
    )


def test_desktop_graph_never_imports_web_stack() -> None:
    """No HTTP server framework or ML stack enters the desktop graph."""
    keys = _child_module_keys()
    banned_prefixes = ("fastapi", "uvicorn", "torch", "sentence_transformers")
    offenders = sorted(
        k
        for k in keys
        if any(k == p or k.startswith(p + ".") for p in banned_prefixes)
    )
    assert offenders == [], (
        "the desktop runtime graph imported web/ML stack modules: "
        f"{offenders}. LocalStorage is the only persistence and the "
        "shell starts no server."
    )


def test_desktop_graph_imports_the_local_modules() -> None:
    """Positive control: the runtime graph does load its own modules.

    Guards against a vacuous pass (e.g. the child failing silently and
    the banned sets trivially holding).
    """
    keys = _child_module_keys()
    for expected in (
        "backend.companion_runtime",
        "backend.desktop.api",
        "backend.desktop.app",
        "backend.local_storage",
    ):
        assert expected in keys, f"expected {expected} in the desktop graph"


@pytest.mark.parametrize("env_var", ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"])
def test_desktop_graph_imports_with_supabase_env_vars_set(env_var: str) -> None:
    """Supabase env vars set in the environment change nothing.

    Requirement 9 of #336: no Supabase token/key is *needed or loaded*.
    The desktop graph must not read them even when they exist (a user
    with an old web install still gets the clean local runtime).
    """
    import os

    env = dict(os.environ)
    env[env_var] = "https://example.invalid/never-contacted"
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    last_line = result.stdout.strip().splitlines()[-1]
    keys = set(json.loads(last_line))
    offenders = sorted(k for k in keys if k == "supabase" or k.startswith("supabase."))
    assert offenders == [], (
        f"supabase modules entered the graph with {env_var} set: {offenders}"
    )
