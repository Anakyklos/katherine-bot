"""Frontend build resolution for the desktop shell (#334, #338).

The desktop entrypoint loads the *real* Vite build output — never a
throwaway page. Resolution:

* is driven by an explicit, constrained config (no caller-supplied path
  strings, no environment expansion, so path traversal is structurally
  impossible);
* fails explicitly with an actionable message when the build is missing;
* performs no filesystem writes.

Installed layout (#338): a ``.deb`` install places the frontend build
at ``/usr/lib/katherine/frontend/dist`` and the Python package at
``/usr/lib/katherine/backend``. The resolver distinguishes exactly two
roots, derived structurally from this file's own location (never from
the CWD, never from the environment, never from a checkout):

* **checkout root** — ``backend/desktop/build_resolver.py`` sits two
  levels below a repository root, whose ``frontend/`` exists next to
  ``backend/`` (development: `python -m backend.desktop.app`);
* **packaged root** — the module lives at
  ``<install>/lib/python…/…/backend/desktop/build_resolver.py`` and the
  frontend build was placed, at package build time, in the sibling
  ``frontend/dist`` directory of the *installed* application root.

The detection is one function (:func:`default_frontend_root`), used by
exactly one caller (``backend.desktop.app``). No other module senses
"packaged mode".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Actionable hint shown when the build is absent (checkout mode).
_BUILD_HINT = "Run 'npm run build' in the frontend/ directory first."

#: Actionable hint shown when the build is absent (installed package).
_BUILD_HINT_PACKAGED = (
    "The installed frontend build is missing. Reinstall the "
    "katherine-desktop package (the build is part of the package)."
)

#: Name of the frontend directory inside either root.
_FRONTEND_DIR_NAME = "frontend"


class BuildResolutionError(Exception):
    """Raised when the frontend build is missing or incomplete.

    The message is safe to display: it names the relative build directory
    and the fix, never absolute local paths.
    """


def default_frontend_root(module_file: Path | None = None) -> Path:
    """Return the frontend root for dev-checkout or installed package (#338).

    The two roots are structural, derived from this module's own path:

    * checkout: ``<repo>/frontend`` — this file is ``<repo>/backend/
      desktop/build_resolver.py`` (parents[2] is the repo root) **and**
      a ``frontend`` directory exists there;
    * installed: ``<app>/frontend`` — this file is somewhere under an
      installed application root that carries a ``frontend`` directory
      as its direct child. The ``.deb`` layout is
      ``/usr/lib/katherine/{backend,frontend}``, where
      ``backend/desktop`` is three levels below the app root
      (``parents[4]``); the search walks upward a bounded number of
      levels looking for the first directory whose direct child is
      ``frontend`` (and that child is not the module's own checkout
      root).

    Never uses the CWD, the environment, or ``sys.path``; never falls
    back to a guess. If no root carries a ``frontend`` directory, the
    last candidate (checkout-style) is returned so the caller fails
    with the actionable checkout hint.
    """
    here = (module_file if module_file is not None else Path(__file__)).resolve()
    # Walk a bounded number of parents: enough for both layouts
    # (checkout: parents[2]; deb layout: parents[4]; plus margin for
    # a future prefix like /opt/katherine/lib/katherine).
    candidates: list[Path] = []
    for parent in here.parents[:6]:
        candidates.append(parent / _FRONTEND_DIR_NAME)
    # Prefer the FIRST (deepest, closest to this file) existing root:
    # in the .deb layout /usr/lib/katherine/frontend is closer than any
    # accidental /frontend that might exist higher up; in the checkout
    # layout <repo>/frontend is the only match. parents[:6] is ordered
    # nearest-first, so the first existing match is the right one.
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # No frontend anywhere: fall back to the checkout-style location so
    # the error message is the actionable dev hint (parent of backend/).
    return here.parents[2] / _FRONTEND_DIR_NAME


def packaged_build_hint() -> str:
    """Hint for a missing build in an installed (non-checkout) root."""
    return _BUILD_HINT_PACKAGED


def _hint_for_root(frontend_root: Path) -> str:
    """Pick the actionable hint matching the root's origin.

    A root is a *checkout* root when this module also lives inside that
    same tree (``<root>/backend/desktop/build_resolver.py`` exists) —
    the developer can run ``npm run build``. Otherwise the root came
    from the installed layout and the fix is reinstalling the package.
    """
    if frontend_root.joinpath("backend", "desktop", "build_resolver.py").is_file():
        return _BUILD_HINT
    return _BUILD_HINT_PACKAGED


@dataclass(frozen=True, slots=True)
class DesktopBuildConfig:
    """Constrained configuration for build resolution.

    ``frontend_root`` must be an existing directory (validated here, so
    resolution can never operate on a guessed or injected path).
    """

    frontend_root: Path

    def __post_init__(self) -> None:
        if not self.frontend_root.is_dir():
            raise ValueError("frontend_root must be an existing directory")


@dataclass(frozen=True, slots=True)
class ResolvedBuild:
    """Absolute, verified locations of the built frontend."""

    dist_dir: Path
    index_html: Path
    desktop_html: Path


def resolve_frontend_build(config: DesktopBuildConfig) -> ResolvedBuild:
    """Resolve and validate the local Vite build for the desktop shell.

    Raises :class:`BuildResolutionError` with an actionable, sanitized
    message when the build does not exist or is incomplete. There is no
    silent fallback: the caller must fail visibly.

    The hint in the error message matches the root's origin: a packaged
    install must not tell the user to run npm inside a package they
    cannot rebuild in place; a checkout must not tell them to reinstall
    a package they do not have.
    """
    dist_dir = config.frontend_root / "dist"
    index_html = dist_dir / "index.html"
    desktop_html = dist_dir / "desktop.html"

    hint = _hint_for_root(config.frontend_root)

    if not dist_dir.is_dir():
        raise BuildResolutionError(
            "Frontend build not found at frontend/dist. " + hint
        )
    if not index_html.is_file():
        raise BuildResolutionError(
            "Frontend build is incomplete (index.html missing). " + hint
        )
    if not desktop_html.is_file():
        raise BuildResolutionError(
            "Frontend build is incomplete (desktop.html missing). " + hint
        )

    return ResolvedBuild(dist_dir=dist_dir, index_html=index_html, desktop_html=desktop_html)
