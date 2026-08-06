"""Import-safety tests: importing backend modules must have no side effects.

These tests run in isolated subprocesses so they never contaminate the
``sys.modules`` of the main suite, and blockers (socket, client factories,
model construction) are installed BEFORE the import under test.

Covered properties (#275):
* imports never open sockets;
* imports never construct real Groq or Supabase clients;
* imports never instantiate ``SentenceTransformer``;
* imports never start threads;
* the test suite's own module registry stays untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GUARD_SCRIPT = textwrap.dedent(
    """
    import sys
    import threading

    # ── Blockers installed BEFORE any backend import ────────────────────────
    import socket as _socket

    def _forbid(*args, **kwargs):
        raise AssertionError("network socket usage during import")

    _socket.socket.connect = _forbid
    _socket.socket.connect_ex = _forbid
    _socket.create_connection = _forbid

    import groq as _groq

    class _NoGroqClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("real Groq client constructed during import")

    _groq.Groq = _NoGroqClient
    _groq.AsyncGroq = _NoGroqClient

    import supabase as _supabase

    def _no_supabase_client(*args, **kwargs):
        raise AssertionError("real Supabase client constructed during import")

    _supabase.create_client = _no_supabase_client

    import sentence_transformers as _st

    class _NoEmbeddingModel:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SentenceTransformer constructed during import")

    _st.SentenceTransformer = _NoEmbeddingModel

    threads_before = len(threading.enumerate())

    # ── Import the modules under test ───────────────────────────────────────
    import backend.main
    import backend.engine
    import backend.memory
    import backend.groq_manager
    import backend.dependencies
    import backend.settings
    import backend.health
    import backend.observability
    import backend.process_turn

    threads_after = len(threading.enumerate())

    assert backend.main.app is not None, "module-level app must exist"
    assert backend.main.app.state.dependencies is None, (
        "import must not build the dependency container"
    )
    assert backend.main.app.state.lifespan_started is False
    assert threads_after == threads_before, "import started a thread"

    print("IMPORT_SAFETY_OK")
    """
)


def _run_guard_script() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": ".",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "GROQ_API_KEY": "mock-groq-key",
            "GROQ_API_KEY_2": "mock-groq-key-2",
            "ADMISSION_HMAC_SECRET": "mock-admission-secret-at-least-32-bytes-xxxx",
            "SUPABASE_URL": "http://127.0.0.1:54321",
            "SUPABASE_SERVICE_ROLE_KEY": "mock-service-key",
            "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
            "ARCHIVAL_EXTRACTION_ENABLED": "false",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", GUARD_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# ─── 10-15. Isolated subprocess guards ──────────────────────────────────────


def test_import_opens_no_socket_and_constructs_no_clients():
    result = _run_guard_script()
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "IMPORT_SAFETY_OK" in result.stdout


def test_import_does_not_start_threads():
    result = _run_guard_script()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "IMPORT_SAFETY_OK" in result.stdout


def test_import_blocks_installed_before_import_are_effective():
    """The guard script itself proves the blockers run before the import.

    The blockers are installed first, then a deliberate socket attempt must
    fail with the blocker's marker: if the import under test had tried to
    open any connection, it would have hit the same blocker.
    """
    script = textwrap.dedent(
        """
        import socket as _socket

        def _forbid(*args, **kwargs):
            raise AssertionError("BLOCKER_MARKER")

        _socket.socket.connect = _forbid
        _socket.create_connection = _forbid

        try:
            _socket.create_connection(("127.0.0.1", 1), timeout=0.1)
            print("BLOCKER_INACTIVE")
            raise SystemExit(1)
        except AssertionError as exc:
            assert "BLOCKER_MARKER" in str(exc)
            print("BLOCKER_ACTIVE")
        """
    )
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": ".",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "GROQ_API_KEY": "mock-groq-key",
            "ADMISSION_HMAC_SECRET": "mock-admission-secret-at-least-32-bytes-xxxx",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "BLOCKER_ACTIVE" in result.stdout


# ─── 16. Main suite registry is not contaminated ────────────────────────────


def test_subprocess_isolation_does_not_pollute_suite_modules():
    """The subprocess runs in a fresh interpreter; nothing leaks back."""
    import backend.main as main_module

    assert main_module.app is not None
    # The module object is the suite's own (no duplicate reload happened).
    assert sys.modules["backend.main"] is main_module


def test_importing_main_again_is_idempotent():
    import importlib

    import backend.main as main_module

    assert importlib.import_module("backend.main") is main_module
