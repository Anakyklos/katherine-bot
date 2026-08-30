"""Desktop entrypoint: opens the Katherine window via pywebview (#334).

Responsibilities (deliberately tiny):

1. resolve the local frontend build (explicit failure if missing);
2. build the allowlisted Desktop API;
3. open one pywebview window pointed at the local build via ``file://``;
4. block until the window closes, then return cleanly.

Not here (on purpose): no domain logic, no ConversationEngine, no
backend server, no HTTP hosting of the UI, no background services.

Lifecycle notes:
* No threads, sockets, servers, or ports are created by this shell.
* Shutdown is the window close: ``webview.start()`` returns and the
  process exits normally through the caller. No ``os._exit``, no kills.
* Navigation policy: the window loads only the local build; this module
  never exposes a "navigate to arbitrary URL" path, so remote content
  cannot reach the privileged bridge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from backend.desktop.api import DESKTOP_API_METHODS, DesktopApi
from backend.desktop.build_resolver import (
    BuildResolutionError,
    DesktopBuildConfig,
    resolve_frontend_build,
)

_WINDOW_TITLE = "Katherine"
_WINDOW_SIZE = (1280, 800)


def _repo_root() -> Path:
    """Repository root derived from this file's location (no env, no cwd)."""
    return Path(__file__).resolve().parents[2]


def run_desktop_shell(frontend_root: Path | None = None) -> int:
    """Open the desktop window and block until it is closed.

    Returns 0 on clean close. Raises :class:`BuildResolutionError` (or
    ``ValueError`` for an invalid root) *before* any window is created,
    so startup failures are explicit and safe to print.
    """
    root = frontend_root if frontend_root is not None else _repo_root() / "frontend"
    config = DesktopBuildConfig(frontend_root=root)
    build = resolve_frontend_build(config)

    api = DesktopApi()
    webview.create_window(
        title=_WINDOW_TITLE,
        url=build.index_html.as_uri(),
        js_api=api,
        width=_WINDOW_SIZE[0],
        height=_WINDOW_SIZE[1],
    )
    webview.start()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint: ``python -m backend.desktop.app``.

    Prints a sanitized, actionable message on predictable startup errors
    (missing build, invalid root) and returns a non-zero exit code.
    Never falls back to opening anything else.
    """
    try:
        return run_desktop_shell()
    except BuildResolutionError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except ValueError as err:
        print(f"error: invalid desktop configuration: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
