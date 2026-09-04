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
   URL; if it is no longer the local build, it *revokes the trust
   first* (:meth:`BuildTrust.revoke`) and then navigates back to the
   entry. The remote document may exist transiently, but cannot be
   interacted with and does not persist.
2. **Fail-closed bridge** — the ``js_api`` object checks the URL on
   *every* call. A call is served only when the current URL is exactly
   the URL of the last *completed and committed* local load. Any
   in-flight navigation — remote, or the revert itself, even when
   ``get_uri()`` already shows the local entry URL again — leaves the
   bridge closed. Even in the race window before the revert completes,
   remote code cannot invoke Python through the bridge.

Both layers are simple and local (no proxy, no HTTP server, no extra
process) and covered by ``backend/tests/test_desktop_navigation.py``
plus the reproducible smoke in ``scripts/desktop_smoke.py``.

Why the trust must be *revoked* before the revert navigation
(reviewer follow-up on the same-URL window): WebKitGTK's ``get_uri()``
reflects the load that *started*. When the revert navigates back to
the very same ``entry_html`` the window opened with, ``get_uri()``
returns the previously committed URL while the **remote document is
still the live document**. URL equality therefore proves neither
document identity nor load completion; the only thing that may
re-open the bridge is the ``loaded`` event of the *new* local load
committing again.
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

#: Public error code used when the local runtime cannot start (corrupt DB,
#: unwritable directory). Sanitized: no path, no traceback.
ERROR_RUNTIME_STARTUP = "runtime_startup_failed"

_MSG_BRIDGE_UNAVAILABLE = "Desktop bridge is not available for this page."
_MSG_INTERNAL = "The desktop bridge failed to complete the request."
_MSG_RUNTIME_STARTUP = (
    "O armazenamento local não pôde ser aberto. "
    "Verifique o banco de dados local e tente novamente."
)


def _build_language_model_factory():
    """Build the desktop provider wiring (issue #337 review).

    The concrete provider choice lives in this composition root, not
    in the runtime: the desktop selects Groq explicitly (no
    auto-routing, no fallback) and reads its keys Python-side only
    (env values are never echoed, never in the Vite bundle, never
    through the bridge). The factory stays lazy — importing the app or
    building the runtime never loads a provider SDK; the adapter is
    constructed on the first turn that needs the model.
    """
    from backend.groq_keys import get_groq_api_keys
    from backend.language_model import resolve_language_model_factory

    keys = tuple(
        k for k in get_groq_api_keys() if isinstance(k, str) and k.strip()
    )
    return resolve_language_model_factory("groq", keys=keys)


def _build_runtime():
    """Build the production companion runtime (#336, #337).

    Lazy import on purpose: ``backend.desktop.app`` stays import-pure
    (no domain modules at import time; the runtime pulls only light,
    pure-domain dependencies — verified by the import-cost budget
    tests). The runtime opens its storage lazily on first use, so this
    call never touches SQLite; it just constructs the object.

    Provider wiring (issue #337 review): the runtime itself is
    provider-agnostic; this composition root supplies the explicit
    Groq factory and the configuration probe (presence-only, never a
    generation call).
    """
    from backend.companion_runtime import build_companion_runtime

    factory = _build_language_model_factory()
    return build_companion_runtime(
        language_model_factory=factory,
        provider_configured_probe=getattr(
            factory, "provider_configured_probe", None
        ),
    )


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
    URL must still equal that committed URL.

    **The same-URL window** (reviewer follow-up): the revert navigates
    back to the very ``entry_html`` the window opened with. Because
    ``get_uri()`` flips as soon as that load starts, ``current_url ==
    committed`` is TRUE again while the remote document is still the
    live document. URL equality proves neither identity nor
    completion, so the trust is *revoked* (:meth:`revoke`) the moment
    a non-local load is reported — BEFORE the revert navigation is
    issued — and only the ``loaded`` event of the new local load
    (a fresh commit) may re-open the bridge. There is no state in
    which an in-flight navigation (remote or revert) serves a call.

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

    def revoke(self) -> None:
        """Drop the committed trust immediately.

        Called the moment a non-local load is reported, BEFORE any
        revert ``load_url()`` is issued: from that instant until the
        new local load completes and is committed again, every bridge
        call fails closed — including calls that arrive while
        ``get_uri()`` already reads the local entry URL (the revert
        load started, the remote document may still be alive).
        """
        with self._lock:
            self._committed = None

    def is_trusted(self, current_url: str | None) -> bool:
        """True iff ``current_url`` is exactly the committed local URL.

        The commit only exists between a *completed* local load and
        the next revocation, so any in-flight navigation (remote page
        alive while a revert load already started — even to the same
        URL — or a local page still loading) fails this check and the
        bridge fails closed.
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

    def _serve(self, op: str, args: tuple[Any, ...]) -> dict[str, Any]:
        """Shared fail-closed wrapper for one allowlisted op."""
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
            result = getattr(self._bridge, op)(*args)
        except DesktopApiError as err:
            return err.payload
        except Exception:  # noqa: BLE001 (boundary: never leak internals to JS)
            return {"ok": False, "code": "internal_error", "message": _MSG_INTERNAL}
        if not isinstance(result, dict):
            return {"ok": False, "code": "internal_error", "message": _MSG_INTERNAL}
        return result

    # -- allowlisted surface (exactly DESKTOP_API_METHODS) -----------------

    def health(self, *args: Any) -> dict[str, Any]:
        """Allowlisted round-trip, local-build-only; never raises."""
        return self._serve("health", args)

    def runtime_state(self, *args: Any) -> dict[str, Any]:
        """Readiness probe, local-build-only; never raises."""
        return self._serve("runtime_state", args)

    def load_history(self, *args: Any) -> dict[str, Any]:
        """Bounded history read, local-build-only; never raises."""
        return self._serve("load_history", args)

    def send_message(self, *args: Any) -> dict[str, Any]:
        """One conversation turn, local-build-only; never raises."""
        return self._serve("send_message", args)

    def delete_history(self, *args: Any) -> dict[str, Any]:
        """Privacy: erase history, local-build-only; never raises."""
        return self._serve("delete_history", args)

    def delete_memories(self, *args: Any) -> dict[str, Any]:
        """Privacy: erase memories, local-build-only; never raises."""
        return self._serve("delete_memories", args)

    def reset_emotional_state(self, *args: Any) -> dict[str, Any]:
        """Privacy: reset emotional state, local-build-only; never raises."""
        return self._serve("reset_emotional_state", args)

    def reset_relationship_state(self, *args: Any) -> dict[str, Any]:
        """Privacy: reset relationship state, local-build-only; never raises."""
        return self._serve("reset_relationship_state", args)


class NavigationPolicy:
    """The ``loaded``-handler logic, extracted to be testable (#334).

    Contract (all transitions explicit, no timing assumptions):

    * local load completed → commit the URL as trusted;
    * non-local load reported → revoke the trust FIRST, then issue the
      revert ``load_url`` to the entry page;
    * while the revert is in flight (``get_uri()`` may already read the
      same entry URL) → the trust stays revoked, so the bridge stays
      closed;
    * new local load completed → the ``loaded`` event commits again and
      the bridge re-opens.

    The object is deliberately passive: no polling, no threads, no
    timers. It acts only when the window fires ``loaded``.
    """

    def __init__(
        self, window: Any, build: ResolvedBuild, entry_uri: str, trust: BuildTrust
    ) -> None:
        self._window = window
        self._build = build
        self._entry_uri = entry_uri
        self._trust = trust

    def on_loaded(self) -> None:
        """Handle one ``loaded`` event (a load completed in the window)."""
        url = _get_url_safely(self._window.get_current_url)
        if is_local_build_url(url, self._build):
            self._trust.commit_if_local(url)
        else:
            # Non-local document reported: drop the trust BEFORE the
            # revert navigation starts. From this instant the bridge is
            # closed for every caller, including one that arrives while
            # get_uri() already reads the same entry URL again (the
            # revert load started, the remote document may still be
            # the live document).
            self._trust.revoke()
            self._window.load_url(self._entry_uri)


def make_navigation_policy(
    window: Any,
    build: ResolvedBuild,
    *,
    entry_uri: str | None = None,
    trust: BuildTrust | None = None,
) -> NavigationPolicy:
    """Build the :class:`NavigationPolicy` for a shell window.

    ``entry_uri`` defaults to ``build.desktop_html.as_uri()`` (the
    desktop companion entry; production passes the page it opened,
    e.g. the smoke page).
    ``trust`` defaults to a fresh :class:`BuildTrust`; production
    passes the one the bridge shares so both layers stay in sync.
    """
    resolved_entry = entry_uri if entry_uri is not None else build.desktop_html.as_uri()
    return NavigationPolicy(
        window=window,
        build=build,
        entry_uri=resolved_entry,
        trust=trust if trust is not None else BuildTrust(build),
    )


def run_desktop_shell(
    frontend_root: Path | None = None,
    *,
    html_name: str = "desktop.html",
    storage_path: Path | str | None = None,
    provider: Any = None,
) -> int:
    """Open the desktop window and block until it is closed.

    Returns 0 on clean close. Raises :class:`BuildResolutionError` (or
    ``ValueError`` for an invalid root) *before* any window is created,
    so startup failures are explicit and safe to print.

    ``html_name`` selects which page inside the resolved build opens
    first. Production uses the default ``desktop.html`` — the desktop
    companion entry whose module graph contains no web modules (#336,
    review blocker 1; web entry is index.html and is never loaded by
    the shell). The reproducible smoke (#334, review B3) opens
    ``desktop-smoke.html`` which mounts the real ChatWindow. The page
    must exist inside ``dist`` (the resolver still validates the build
    root), and the navigation policy treats every page inside ``dist``
    as local build content.

    ``storage_path`` / ``provider`` are smoke-test seams only (#336):
    the reproducible smoke must not touch the user's real database or
    spend real provider quota. Production callers never pass them —
    the default path and the real Groq provider factory stay exactly
    as they are.
    """
    root = frontend_root if frontend_root is not None else _repo_root() / "frontend"
    config = DesktopBuildConfig(frontend_root=root)
    build = resolve_frontend_build(config)

    entry_html = build.dist_dir / html_name
    if not entry_html.is_file():
        raise BuildResolutionError(
            f"Frontend page {html_name!r} not found in the build. " + _BUILD_HINT
        )

    # Runtime lifecycle (#336): constructed before the window so the
    # bridge it serves is wired exactly once; closed after the window
    # loop returns (window close ⇒ clean shutdown, always, via finally).
    if storage_path is None and provider is None:
        runtime = _build_runtime()
    else:
        from backend.companion_runtime import CompanionRuntime

        runtime = CompanionRuntime(
            storage_path=storage_path,
            language_model=provider,
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

    js_api = LocalBuildBridge(make_js_api(runtime=runtime), build, _current_url, trust)

    window = webview.create_window(
        title=_WINDOW_TITLE,
        url=entry_html.as_uri(),
        js_api=js_api,
        width=_WINDOW_SIZE[0],
        height=_WINDOW_SIZE[1],
    )
    window_holder.append(window)

    # Policy layer 1: on every completed load, commit local pages as
    # trusted and revert non-local navigation back to the build. A
    # non-local load REVOKES the trust before the revert ``load_url``
    # is issued, so the bridge stays closed for the whole revert —
    # including the window where get_uri() already reads the same
    # entry URL while the remote document is still the live one.
    policy = make_navigation_policy(
        window=window,
        build=build,
        entry_uri=entry_html.as_uri(),
        trust=trust,
    )
    window.events.loaded += policy.on_loaded

    try:
        webview.start()
    finally:
        # Window closed (or the loop raised): shut the runtime down
        # cleanly. Idempotent and never raises.
        runtime.close()
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
    except Exception:  # noqa: BLE001 (startup boundary: sanitized output only)
        # Any unexpected startup failure (corrupt local database,
        # unwritable storage): print the constant, sanitized message —
        # never a path, traceback or internal detail — and exit 2.
        print(f"error: {_MSG_RUNTIME_STARTUP}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
