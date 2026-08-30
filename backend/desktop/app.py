"""Desktop entrypoint: opens the Katherine window via pywebview (#334).

Responsibilities (deliberately tiny):

1. resolve the local frontend build (explicit failure if missing);
2. build the sanitized, allowlisted desktop bridge (``make_js_api``);
3. open one pywebview window pointed at the local build via ``file://``;
4. enforce the navigation policy below;
5. block until the window closes, then return cleanly.

Not here (on purpose): no domain logic, no ConversationEngine, no
backend server, no HTTP hosting of the UI, no background services.

Lifecycle notes:
* No threads, sockets, servers, or ports are created by this shell.
* Shutdown is the window close: ``webview.start()`` returns and the
  process exits normally through the caller. No ``os._exit``, no kills.

Navigation policy (remote content must never gain the privileged bridge)
---------------------------------------------------------------------------

pywebview injects ``window.pywebview`` into *whatever document the
webview loaded last* (WebKitGTK re-injects on every ``load-changed`` /
FINISHED). Loading ``file://`` initially is therefore not enough: a
same-window ``location`` change (link click, ``window.location = ...``)
would hand ``window.pywebview.api`` to remote content.

This module enforces two independent, local layers:

1. **Revert navigation** — a ``loaded`` event handler checks the window
   URL; if it is no longer the local build, it navigates back
   immediately. The remote document may exist transiently, but cannot
   be interacted with and does not persist.
2. **Fail-closed bridge** — the ``js_api`` object checks the URL on
   *every* call. If the current window URL is not the local build,
   calls return a sanitized ``bridge_unavailable`` payload instead of
   executing. Even in the race window before the revert completes,
   remote code cannot invoke Python through the bridge.

Both layers are simple and local (no proxy, no HTTP server, no extra
process) and covered by ``backend/tests/test_desktop_navigation.py``
plus the reproducible smoke in ``scripts/desktop_smoke.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import webview

from backend.desktop.api import DESKTOP_API_METHODS, DesktopApiError, make_js_api
from backend.desktop.build_resolver import (
    BuildResolutionError,
    DesktopBuildConfig,
    ResolvedBuild,
    resolve_frontend_build,
)

_WINDOW_TITLE = "Katherine"
_WINDOW_SIZE = (1280, 800)

#: Actionable hint reused for a missing smoke page inside the build.
_BUILD_HINT = "Run 'npm run build' in the frontend/ directory first."

#: Public error code used when the bridge refuses to serve a non-local page.
ERROR_BRIDGE_UNAVAILABLE = "bridge_unavailable"

_MSG_BRIDGE_UNAVAILABLE = "Desktop bridge is not available for this page."
_MSG_INTERNAL = "The desktop bridge failed to complete the request."


def _repo_root() -> Path:
    """Repository root derived from this file's location (no env, no cwd)."""
    return Path(__file__).resolve().parents[2]


def is_local_build_url(url: str | None, build: ResolvedBuild) -> bool:
    """Return True iff ``url`` points inside the resolved local build.

    Structural check (not string-prefix based): the URL must be
    ``file://`` with an empty host, no query string and no fragment, and
    its path must resolve (lexically, no filesystem access) inside the
    build's ``dist`` directory or be the ``index.html`` itself. Pure
    predicate: no network, no filesystem access, never raises.
    """
    if not url or not url.startswith("file://"):
        return False
    try:
        split = urlsplit(url)
    except ValueError:  # pragma: no cover - defensive: malformed URLs
        return False
    if split.scheme != "file" or split.netloc:
        return False
    if split.query or split.fragment:
        return False
    dist_dir = build.dist_dir
    index_html = build.index_html
    if split.path == index_html.as_posix():
        return True
    candidate = Path(split.path)
    try:
        candidate.relative_to(dist_dir)
    except ValueError:
        return False
    # Reject lexical traversal: any ``..``/``.`` segment means the URL
    # does not literally point inside dist (e.g. ``dist/../x.html``).
    # Raw ``split.path`` is used: ``Path`` would normalize the ``..``
    # segments away and defeat this check.
    segments = split.path.split("/")
    if ".." in segments or "." in segments:
        return False
    return split.path.startswith(dist_dir.as_posix() + "/")


def _get_url_safely(get_url: Callable[[], str | None]) -> str | None:
    """Call ``get_url()`` defensively; None on any failure."""
    try:
        return get_url()
    except Exception:  # noqa: BLE001 (policy layer must never crash the shell)
        return None


class BuildTrust:
    """The URL of the last *completed, local* load (#334, review B2).

    Why this exists (race found during validation): WebKitGTK's
    ``get_uri()`` reflects the most recent load that *started*, not the
    document that is actually alive. During the revert navigation
    (remote → local) there is a window where ``get_uri()`` is already
    the local ``file://`` URL while the **remote document is still
    alive** and could invoke the bridge. A URL check alone would pass
    in that window.

    The trust model closes it: only a URL whose load *completed* as a
    local build page (committed by the ``loaded`` handler, which also
    reverts non-local pages) may serve bridge calls, and the current
    URL must still equal that committed URL. Any in-flight navigation
    (remote or back) leaves the two out of sync, so the bridge refuses.

    Thread-safety: ``loaded`` handlers run on the GUI thread; bridge
    calls arrive on worker threads (WebKit dispatch). A simple lock
    guards the committed value.
    """

    def __init__(self, build: ResolvedBuild) -> None:
        import threading

        self._build = build
        self._lock = threading.Lock()
        self._committed: str | None = None

    def commit_if_local(self, url: str | None) -> bool:
        """Commit ``url`` as trusted iff it is a local build page.

        Called from the ``loaded`` handler (load completed). Returns
        True when the URL was committed as trusted.
        """
        if not is_local_build_url(url, self._build):
            with self._lock:
                self._committed = None
            return False
        with self._lock:
            self._committed = url
        return True

    def is_trusted(self, current_url: str | None) -> bool:
        """True iff ``current_url`` is exactly the committed local URL.

        Any in-flight navigation (remote page alive while a revert load
        already started, or a local page still loading) fails this
        check, so the bridge fails closed.
        """
        with self._lock:
            committed = self._committed
        if committed is None:
            return False
        return current_url == committed and is_local_build_url(current_url, self._build)


class LocalBuildBridge:
    """``js_api`` facade that fails closed outside the local build (#334).

    Wraps the sanitized :class:`~backend.desktop.api.DesktopBridge` with
    the navigation policy: every call first verifies that the window is
    still showing the trusted local build page (see :class:`BuildTrust`).
    If not, the call returns a sanitized ``bridge_unavailable`` payload —
    remote content never reaches the underlying Python methods, including
    during the revert race window (remote document alive while the
    revert load already changed ``get_uri()``).

    The public surface stays exactly ``DESKTOP_API_METHODS`` (pywebview
    exposes every public attribute), and no public method ever raises
    (pywebview would convert exceptions into JS ``Error`` objects
    carrying stacktrace information).
    """

    def __init__(
        self,
        bridge: Any,
        build: ResolvedBuild,
        get_url: Callable[[], str | None],
        trust: BuildTrust | None = None,
    ) -> None:
        # ``bridge`` is the sanitized facade from make_js_api(); typed Any
        # to keep this layer decoupled from the concrete facade class.
        self._bridge = bridge
        self._build = build
        self._get_url = get_url
        self._trust = trust if trust is not None else BuildTrust(build)

    def health(self, *args: Any) -> dict[str, Any]:
        """Allowlisted round-trip, local-build-only; never raises."""
        url = _get_url_safely(self._get_url)
        if not (
            is_local_build_url(url, self._build) and self._trust.is_trusted(url)
        ):
            return {
                "ok": False,
                "code": ERROR_BRIDGE_UNAVAILABLE,
                "message": _MSG_BRIDGE_UNAVAILABLE,
            }
        try:
            result = self._bridge.health(*args)
        except DesktopApiError as err:
            return err.payload
        except Exception:  # noqa: BLE001 (boundary: never leak internals to JS)
            return {"ok": False, "code": "internal_error", "message": _MSG_INTERNAL}
        if not isinstance(result, dict):
            return {"ok": False, "code": "internal_error", "message": _MSG_INTERNAL}
        return result


def run_desktop_shell(
    frontend_root: Path | None = None,
    *,
    html_name: str = "index.html",
) -> int:
    """Open the desktop window and block until it is closed.

    Returns 0 on clean close. Raises :class:`BuildResolutionError` (or
    ``ValueError`` for an invalid root) *before* any window is created,
    so startup failures are explicit and safe to print.

    ``html_name`` selects which page inside the resolved build opens
    first. Production uses the default ``index.html``; the reproducible
    smoke (#334, review B3) opens ``desktop-smoke.html`` which mounts
    the real ChatWindow. The page must exist inside ``dist`` (the
    resolver still validates the build root), and the navigation
    policy treats every page inside ``dist`` as local build content.
    """
    root = frontend_root if frontend_root is not None else _repo_root() / "frontend"
    config = DesktopBuildConfig(frontend_root=root)
    build = resolve_frontend_build(config)

    entry_html = build.dist_dir / html_name
    if not entry_html.is_file():
        raise BuildResolutionError(
            f"Frontend page {html_name!r} not found in the build. " + _BUILD_HINT
        )

    # ``create_window`` returns the Window synchronously, before
    # ``webview.start()``; the holder is populated right after creation so
    # the URL guard can query it. The bridge is delivered at creation time
    # (js_api=), never reassigned afterwards.
    window_holder: list[Any] = []
    trust = BuildTrust(build)

    def _current_url() -> str | None:
        if not window_holder:
            return None
        return _get_url_safely(window_holder[0].get_current_url)

    js_api = LocalBuildBridge(make_js_api(), build, _current_url, trust)

    window = webview.create_window(
        title=_WINDOW_TITLE,
        url=entry_html.as_uri(),
        js_api=js_api,
        width=_WINDOW_SIZE[0],
        height=_WINDOW_SIZE[1],
    )
    window_holder.append(window)

    # Policy layer 1: on every completed load, commit local pages as
    # trusted and revert non-local navigation back to the build. The
    # commit happens after the revert decision: a remote page never
    # becomes trusted, and the bridge (which requires the committed
    # URL to match the current one) stays closed during the revert.
    def _on_loaded() -> None:
        url = _get_url_safely(window.get_current_url)
        if is_local_build_url(url, build):
            trust.commit_if_local(url)
        else:
            # Navigate back to the local build; the remote page is
            # transient and the fail-closed bridge covered the race.
            window.load_url(entry_html.as_uri())

    window.events.loaded += _on_loaded

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
