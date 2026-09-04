#!/usr/bin/env python3
"""Build the Katherine desktop `.deb` package (#338).

Single distribution format for this issue: a Debian package targeting
the Debian/Ubuntu family (the validated desktop platform), reusing the
existing desktop runtime — no second tree, no Electron/Tauri, no HTTP
server, no daemon.

Installed layout (all paths under /usr, no /opt, no home):

    /usr/bin/katherine                  entrypoint (wrapper script)
    /usr/lib/katherine/backend/...     the existing Python package
    /usr/lib/katherine/frontend/dist/   the production Vite build
    /usr/lib/katherine/vendor/          pip wheels (deps-only, see below)
    /usr/share/applications/katherine.desktop
    /usr/share/doc/katherine/copyright

User data stays exclusively in XDG paths (~/.local/share/katherine) —
created by the app at first run, never by the package. Upgrades and
removals never touch katherine.db (conffiles are not used; there is
nothing to configure in /etc).

Python runtime strategy (why wheels and not a venv):
  - The package must run on stock distro Python (python3.12 on the
    validated target, Ubuntu 24.04) with the SYSTEM PyGObject stack
    (python3-gi), which pip cannot provide reliably — this mirrors the
    documented dev setup (venv with --system-site-packages).
  - Pure-Python and manylinux wheels are unpacked under
    /usr/lib/katherine/vendor and put on sys.path by the entrypoint,
    ONLY when running the installed app. The vendor set is locked in
    packaging/requirements-desktop.txt and contains exclusively the
    desktop runtime dependency closure (no FastAPI/Supabase/torch —
    enforced by packaging tests).
  - proxy-tools has no wheel on PyPI; its sdist is a single pure-Python
    package, so the build script unpacks it from the downloaded sdist
    (source inspection is part of this script's determinism).

Reproducibility:
  - Version comes from --version (CI passes a fixed string); files are
    copied with a stable walk order; dpkg-deb builds with deterministic
    mtime (SOURCE_DATE_EPOCH) where supported by the environment.
  - The frontend dist must be pre-built (npm run build) — this script
    fails if it is missing instead of rebuilding silently.

Usage:
    python3 packaging/build_deb.py --version 1.0.0~test1 [--out-dir dist/deb]

Outputs:
    <out-dir>/katherine-desktop_<version>_all.deb
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Package name (Debian): -desktop suffix keeps it distinct from any
#: future server package without implying a second app.
PACKAGE = "katherine-desktop"

#: Where the application lives once installed (FHS: architecture-independent
#: application data belongs in /usr/lib).
APP_DIR = Path("usr/lib/katherine")

#: System dependencies expressed as Debian package names. These are the
#: REAL runtime requirements of the pywebview GTK shell (see
#: docs/operations/desktop-shell-linux.md): PyGObject from the system
#: (python3-gi, validated 3.48), WebKitGTK 4.1 runtime + typelib, GTK 3.
#: python3 itself is implicit via the interpreter dependency but stated
#: explicitly for clarity. No recommends, no suggests: nothing else is
#: needed for the desktop app to run.
DEPENDS = [
    "python3 (>= 3.12)",
    "python3-gi (>= 3.48)",
    "gir1.2-webkit2-4.1",
    "libwebkit2gtk-4.1-0",
    "libgtk-3-0",
    "libglib2.0-0",
]

#: Wheel/sdist names that must NEVER appear in the vendor tree — the
#: desktop closure is small on purpose (import isolation tests prove the
#: runtime never needs the cloud stack; the package must not ship it).
BANNED_VENDOR_PREFIXES = (
    "fastapi",
    "uvicorn",
    "supabase",
    "torch",
    "sentence_transformers",
    "sentence_transformers-",  # wheel name normalization
    "postgrest",
    "storage3",
    "realtime",
    "gotrue",
    "numpy",
)

#: Backend submodules that the desktop needs at runtime. The desktop
#: graph is backend.desktop + backend.companion_runtime + its imports
#: (verified by backend/tests/test_desktop_import_isolation.py, whose
#: child-process module list this list must mirror); everything else
#: in backend/ is legacy web/cloud code that stays in the repository
#: (untouched) but is NOT installed. Listing explicit FILES keeps the
#: package minimal while copying the real files (no second
#: implementation). NOTE: `backend` itself is a PEP 420 namespace
#: package — no __init__.py exists and none is shipped.
DESKTOP_BACKEND_FILES = [
    "backend/admission_contracts.py",
    "backend/companion_runtime.py",
    "backend/desktop/__init__.py",
    "backend/desktop/api.py",
    "backend/desktop/app.py",
    "backend/desktop/build_resolver.py",
    "backend/emotional_core.py",
    "backend/emotion_presentation.py",
    "backend/emotional_domain/__init__.py",
    "backend/emotional_domain/appraisal_parser.py",
    "backend/emotional_domain/migration.py",
    "backend/emotional_domain/models.py",
    "backend/emotional_domain/serialization.py",
    "backend/emotional_domain/transition.py",
    "backend/groq_keys.py",
    "backend/groq_language_model.py",
    "backend/groq_manager.py",
    "backend/language_model.py",
    "backend/local_storage/__init__.py",
    "backend/local_storage/contracts.py",
    "backend/local_storage/errors.py",
    "backend/local_storage/legacy_import.py",
    "backend/local_storage/migrations.py",
    "backend/local_storage/storage.py",
    "backend/provider_envelope.py",
    "backend/provider_models.py",
    "backend/relationship.py",
    "backend/trusted_context.py",
    "backend/trusted_policy.py",
    "backend/turn_execution.py",
]

#: Entrypoint wrapper installed at /usr/bin/katherine. Minimal and
#: explicit around backend.desktop.app: it fixes sys.path to the
#: installed app (code + vendored wheels), never touches CWD, and
#: execs the SAME desktop shell. The app dir is derived from $0 so
#: the entrypoint stays correct if the tree is relocated (also how
#: the isolated install harness reaches it under /install-root).
#: No server, no daemon, no env dependence.
ENTRYPOINT = """#!/bin/sh
# Katherine desktop entrypoint (installed package, #338).
# Runs the same backend.desktop.app shell as the checkout; the only
# additions are sys.path setup for the installed code and vendored
# dependencies. No CWD dependence, no server, no daemon.
set -e
SELF=$(readlink -f "$0")
APP=$(dirname "$(dirname "$SELF")")/lib/katherine
exec python3 -c 'import sys; sys.path.insert(0, "'"$APP"'/vendor"); sys.path.insert(0, "'"$APP"'"); from backend.desktop.app import main; sys.exit(main())' "$@"
"""

#: XDG .desktop launcher (menu integration; no extra runtime dep).
DESKTOP_FILE = """[Desktop Entry]
Type=Application
Name=Katherine
GenericName=AI Companion
Comment=Local-first AI companion (chat, private by default)
Exec=katherine
Icon=katherine
Terminal=false
Categories=Network;InstantMessaging;
Keywords=chat;ai;companion;
"""


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def normalize_stage_mtimes(stage: Path, epoch: int) -> None:
    """Set every staging entry's mtime to the reproducibility epoch.

    ``dpkg-deb`` archives the mtimes present in the staging tree.  Source
    checkouts, downloaded wheels, and frontend output otherwise carry
    wall-clock mtimes into the archive, making identical builds differ.
    Symlinks are never followed so this cannot mutate anything outside
    the staging tree.
    """
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
    timestamp_ns = epoch * 1_000_000_000
    for path in sorted(stage.rglob("*"), key=lambda value: str(value)):
        os.utime(path, ns=(timestamp_ns, timestamp_ns), follow_symlinks=False)
    os.utime(stage, ns=(timestamp_ns, timestamp_ns), follow_symlinks=False)


def normalize_stage_modes(stage: Path) -> None:
    """Set archive entries to ordinary Debian directory and file modes."""
    stage.chmod(0o755)
    for path in sorted(stage.rglob("*"), key=lambda value: str(value)):
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() else 0o644)
    entrypoint = stage / "usr/bin/katherine"
    if entrypoint.is_file():
        entrypoint.chmod(0o755)


def parse_lock(lock_path: Path) -> list[tuple[str, str]]:
    """Parse packaging/requirements-desktop.txt into (name, version)."""
    pins: list[tuple[str, str]] = []
    for line in lock_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins.append((name.strip(), version.strip()))
    if not pins:
        fail(f"no pins found in {lock_path}")
    return pins


def download_wheels(
    pins: list[tuple[str, str]], dest: Path, pip: list[str]
) -> list[Path]:
    """Download the locked dependency closure as wheels (sdist fallback
    only for packages without wheels, e.g. proxy-tools)."""
    reqs = [f"{n}=={v}" for n, v in pins]
    run(
        [*pip, "download", *reqs, "-d", str(dest), "--no-deps"],
        stdout=subprocess.DEVNULL,
    )
    files = sorted(dest.iterdir())
    wheels = [f for f in files if f.name.endswith(".whl")]
    sdists = [f for f in files if f.name.endswith((".tar.gz", ".zip"))]
    # Everything else (e.g. pip metadata leftovers) is a build error.
    rest = [f for f in files if f not in wheels and f not in sdists]
    if rest:
        fail(f"unexpected download artifact: {[f.name for f in rest]}")
    return wheels + sdists


def unpack_wheel(whl: Path, vendor_dir: Path) -> None:
    """Unpack a wheel into the vendor dir (pip wheel layout on disk)."""
    with zipfile.ZipFile(whl) as zf:
        zf.extractall(vendor_dir)
    # Wheels may carry a .dist-info RECORD referencing absolute paths;
    # RECORD is informational only, but drop it to avoid stale hashes.
    for record in vendor_dir.rglob("RECORD"):
        record.unlink()


def unpack_sdist(sdist: Path, vendor_dir: Path) -> None:
    """Extract the single package directory from an sdist.

    Supported case: pure-Python sdists whose tarball contains exactly
    one top-level package directory (proxy-tools). Anything more
    complex fails the build — no silent building.
    """
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        if sdist.name.endswith(".tar.gz"):
            with tarfile.open(sdist) as tf:
                tf.extractall(tdp, filter="data")
        else:
            fail(f"unsupported sdist format: {sdist}")
        # Find top-level package dirs (exclude the sdist root folder).
        roots = [p for p in tdp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            fail(f"sdist {sdist.name} has unexpected layout: {[r.name for r in roots]}")
        pkg_dirs = [
            p
            for p in roots[0].iterdir()
            if p.is_dir() and (p / "__init__.py").is_file()
        ]
        egg_infos = [
            p for p in roots[0].iterdir() if p.is_dir() and p.name.endswith(".egg-info")
        ]
        if len(pkg_dirs) != 1 or not egg_infos:
            fail(f"sdist {sdist.name}: expected exactly one package dir + egg-info")
        shutil.copytree(pkg_dirs[0], vendor_dir / pkg_dirs[0].name)


def guard_vendor(vendor_dir: Path) -> None:
    """Refuse banned dependencies in the vendor tree (fail closed)."""
    top = sorted(p.name for p in vendor_dir.iterdir())
    for name in top:
        normalized = name.lower().replace("_", "-")
        for banned in BANNED_VENDOR_PREFIXES:
            b = banned.rstrip("-").replace("_", "-")
            if normalized == b or normalized.startswith(b + "-"):
                fail(
                    f"banned dependency in vendor tree: {name} "
                    f"(desktop package must not ship the cloud/ML stack)"
                )


def build(
    version: str,
    out_dir: Path,
    pip: list[str],
    keep_stage: Path | None = None,
) -> Path:
    # ── inputs ────────────────────────────────────────────────────────
    frontend_dist = REPO_ROOT / "frontend" / "dist"
    if not (frontend_dist / "desktop.html").is_file():
        fail(
            "frontend/dist/desktop.html not found — run 'npm run build' in "
            "frontend/ first (the production build is packaged, never a stub)"
        )
    lock = parse_lock(REPO_ROOT / "packaging" / "requirements-desktop.txt")

    stage = keep_stage or Path(tempfile.mkdtemp(prefix="katherine-deb-"))
    stage.mkdir(parents=True, exist_ok=True)

    app = stage / APP_DIR
    app.mkdir(parents=True)

    # ── code (the real backend, desktop subset) ─────────────────────
    for rel in DESKTOP_BACKEND_FILES:
        src = REPO_ROOT / rel
        if not src.is_file():
            fail(f"missing backend file for packaging: {rel}")
        dst = stage / APP_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # ── frontend production build ────────────────────────────────────
    # The desktop shell opens desktop.html only (its module graph has
    # no web chunks — enforced by frontend/tests/desktopGraph.test.js).
    # We still ship index.html + the web chunks that the resolver's
    # build validation expects, keeping the packaged build identical to
    # the checkout build (resolver validates index.html presence); the
    # desktop never loads them. The smoke page is a validation tool,
    # not part of the product: excluded. dist may be rebuilt on upgrade.
    dist_dst = stage / APP_DIR / "frontend" / "dist"
    dist_dst.parent.mkdir(parents=True)
    shutil.copytree(frontend_dist, dist_dst, ignore=shutil.ignore_patterns("desktop-smoke.html"))
    (dist_dst / "desktop-smoke.html").unlink(missing_ok=True)

    # ── vendored wheels (deps-only closure) ─────────────────────────
    with tempfile.TemporaryDirectory(prefix="katherine-wheels-") as wd:
        wheels = download_wheels(lock, Path(wd), pip)
        vendor = stage / APP_DIR / "vendor"
        vendor.mkdir()
        for f in wheels:
            if f.name.endswith(".whl"):
                unpack_wheel(f, vendor)
            else:
                unpack_sdist(f, vendor)
        guard_vendor(vendor)

    # ── entrypoint + desktop file ───────────────────────────────────
    bin_dir = stage / "usr/bin"
    bin_dir.mkdir(parents=True)
    entry = bin_dir / "katherine"
    entry.write_text(ENTRYPOINT)
    entry.chmod(0o755)

    apps = stage / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "katherine.desktop").write_text(DESKTOP_FILE)

    doc = stage / "usr/share/doc" / PACKAGE
    doc.mkdir(parents=True)
    (doc / "copyright").write_text(
        "Katherine desktop application.\n"
        "See repository licensing (AGENTS.md / LICENSE) for details.\n"
    )

    # ── icons (from repo assets, if present) ─────────────────────────
    icon_src = REPO_ROOT / "docs" / "assets" / "katherine.png"
    if icon_src.is_file():
        for size, dirname in ((512, "512x512"), (256, "256x256"), (128, "128x128")):
            d = stage / f"usr/share/icons/hicolor/{dirname}/apps"
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(icon_src, d / "katherine.png")

    # ── DEBIAN/control ──────────────────────────────────────────────
    installed_size = sum(
        f.stat().st_size for f in stage.rglob("*") if f.is_file()
    ) // 1024
    debian = stage / "DEBIAN"
    debian.mkdir()
    (debian / "control").write_text(
        f"Package: {PACKAGE}\n"
        f"Version: {version}\n"
        "Architecture: all\n"
        'Maintainer: Katherine maintainers <maintainers@katherine.invalid>\n'
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Installed-Size: {installed_size}\n"
        "Section: x11\n"
        "Priority: optional\n"
        "Homepage: https://github.com/Anakyklos/katherine-bot\n"
        "Description: Katherine local-first desktop companion\n"
        " Native GTK window (pywebview/WebKitGTK) hosting the\n"
        " production frontend build via file://. Local SQLite storage,\n"
        " optional remote LLM provider, no server, no daemon.\n"
    )
    # conffiles: deliberately none. No /etc config exists; user data and
    # settings live in XDG dirs owned by the app at runtime.

    # ── build the .deb ───────────────────────────────────────────────
    # Normalize after every copy/write operation. shutil.copy2 preserves
    # source modes and mkdir follows the caller umask, so doing this only
    # before staging would leave group-writable archive entries.
    normalize_stage_modes(stage)
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        source_date_epoch = int(raw_epoch)
    except ValueError:
        fail("SOURCE_DATE_EPOCH must be a non-negative integer")
    if source_date_epoch < 0:
        fail("SOURCE_DATE_EPOCH must be a non-negative integer")
    normalize_stage_mtimes(stage, source_date_epoch)

    out_dir.mkdir(parents=True, exist_ok=True)
    deb_name = f"{PACKAGE}_{version}_all.deb"
    deb_path = out_dir / deb_name
    env = {
        "TMPDIR": "/tmp",
        "PATH": "/usr/bin:/bin",
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
    }
    run(
        ["dpkg-deb", "--root-owner-group", "--build", str(stage), str(deb_path)],
        env=env,
        cwd=str(stage),
    )
    print(f"built: {deb_path}")
    print(f"  size: {deb_path.stat().st_size / 1024:.0f} KiB")
    return deb_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        help="package version string (e.g. 0.1.0~build1)",
    )
    parser.add_argument(
        "--out-dir",
        default="dist/deb",
        help="output directory for the .deb (default: dist/deb)",
    )
    parser.add_argument(
        "--pip",
        default=sys.executable,
        help="python interpreter whose pip downloads wheels (default: this one)",
    )
    parser.add_argument(
        "--keep-stage",
        default=None,
        help="reuse/keep a staging directory instead of a temp one (debugging)",
    )
    args = parser.parse_args(argv)

    out = Path(args.out_dir)
    if not out.is_absolute():
        out = REPO_ROOT / out
    keep = Path(args.keep_stage) if args.keep_stage else None
    build(args.version, out, [args.pip, "-m", "pip"], keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
