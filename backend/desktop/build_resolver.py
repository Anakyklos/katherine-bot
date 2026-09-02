"""Frontend build resolution for the desktop shell (#334).

The desktop entrypoint loads the *real* Vite build output
(``frontend/dist``) — never a throwaway page. Resolution:

* is driven by an explicit, constrained config (no caller-supplied path
  strings, no environment expansion, so path traversal is structurally
  impossible);
* fails explicitly with an actionable message when the build is missing;
* performs no filesystem writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Actionable hint shown when the build is absent.
_BUILD_HINT = "Run 'npm run build' in the frontend/ directory first."


class BuildResolutionError(Exception):
    """Raised when the frontend build is missing or incomplete.

    The message is safe to display: it names the relative build directory
    and the fix, never absolute local paths.
    """


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
    """
    dist_dir = config.frontend_root / "dist"
    index_html = dist_dir / "index.html"
    desktop_html = dist_dir / "desktop.html"

    if not dist_dir.is_dir():
        raise BuildResolutionError(
            "Frontend build not found at frontend/dist. " + _BUILD_HINT
        )
    if not index_html.is_file():
        raise BuildResolutionError(
            "Frontend build is incomplete (index.html missing). " + _BUILD_HINT
        )
    if not desktop_html.is_file():
        raise BuildResolutionError(
            "Frontend build is incomplete (desktop.html missing). " + _BUILD_HINT
        )

    return ResolvedBuild(dist_dir=dist_dir, index_html=index_html, desktop_html=desktop_html)
