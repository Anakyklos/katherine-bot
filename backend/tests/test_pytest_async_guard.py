"""Regression proof: an unmarked async test cannot be silently skipped.

With ``asyncio_mode = strict`` and the narrow fail-fast guard in pytest.ini
(``error::pytest.PytestUnhandledCoroutineWarning``), any ``async def`` test
collected without ``@pytest.mark.asyncio`` must fail the run instead of being
silently skipped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTEST_INI = REPO_ROOT / "pytest.ini"


def test_unmarked_async_test_fails_instead_of_skipping(tmp_path: Path) -> None:
    probe = tmp_path / "test_unmarked_async_probe.py"
    probe.write_text(
        "import asyncio\n"
        "async def test_unmarked():\n"
        "    await asyncio.sleep(0)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-c",
            str(PYTEST_INI),
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"unmarked async test passed silently:\n{output}"
    assert "PytestUnhandledCoroutineWarning" in output, output
