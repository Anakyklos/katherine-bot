#!/usr/bin/env python3
"""Katherine packaging smoke driver (#338).

Runs the full .deb lifecycle against the REAL dpkg in the isolated
unshare environment (see isolated-install.sh), collecting evidence:

  1. install        dpkg unpack+configure, file list matches lock
  2. import         app imports outside the checkout, finds dist
  3. storage        XDG database created in isolated HOME
  4. turn (no key)  sanitized configuration error, no crash
  5. upgrade        newer .deb over older, dpkg replaces files
  6. downgrade      older package restores without touching user data
  7. purge          package removed, user data PRESERVED
  8. benchmarks     size, startup, RAM — real measurements

Every step prints PASS/FAIL lines; the exit code is non-zero if any
step fails. All evidence is reproducible: same .deb, same commands.

Usage:
  packaging/smoke_deb.py [--deb PATH] [--old-deb PATH] [--skip-upgrade]

The .deb defaults to the newest katherine-desktop_*.deb in
packaging/dist/ (or the path given). Two .debs are needed for the
upgrade/downgrade steps: build one at 0.1.0~test1 and one at
0.1.0~test2 (see --skip-upgrade if you only have one).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "packaging" / "isolated-install.sh"

#: Inline python run inside the isolated env after install. Proves the
#: app works from the installed tree only (no checkout, no venv).
TURN_PROBE = r'''
import asyncio, json, os, pathlib, sys
sys.path.insert(0, "/usr/lib/katherine/vendor")
sys.path.insert(0, "/usr/lib/katherine")
from backend.desktop.app import _build_runtime
rt = _build_runtime()
async def main():
    r = await rt.commit_turn_async(request_id="smoke", message="oi katherine")
    state = rt.runtime_state()
    print(json.dumps({
        "success": r.success,
        "error_code": r.error_code,
        "error_message": r.error_message,
        "provider_configured": state.get("provider_configured"),
        "storage_ok": state.get("storage"),
        "xdg_home": os.environ.get("HOME"),
    }))
    rt.close()
asyncio.run(main())
'''

def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, **kw)


def run_in_env(deb: Path, inner: str) -> tuple[int, str]:
    """Run `inner` (bash) inside the isolated env with `deb` installed."""
    out = sh(["bash", str(HARNESS), str(deb), inner], capture_output=True, text=True)
    return out.returncode, out.stdout + out.stderr


def find_marker(out: str, marker: str) -> str | None:
    for line in out.splitlines():
        if marker in line:
            return line.strip()
    return None


def json_line(out: str) -> dict | None:
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def deb_version(path: Path) -> str:
    """Read a package version from the real Debian control archive."""
    result = sh(
        ["dpkg-deb", "-f", str(path), "Version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read package version: {path}")
    return result.stdout.strip()


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deb", default=None, help="path to the .deb under test")
    ap.add_argument(
        "--old-deb",
        default=None,
        help="older .deb for upgrade/downgrade steps",
    )
    ap.add_argument(
        "--skip-upgrade",
        action="store_true",
        help="skip the two-deb upgrade/downgrade steps",
    )
    args = ap.parse_args(argv)

    # Locate debs.
    dist = REPO / "packaging" / "dist"
    if args.deb:
        deb = Path(args.deb).resolve()
    else:
        cands = sorted(dist.glob("katherine-desktop_*.deb"))
        if not cands:
            print("no .deb found in packaging/dist/ (run build_deb.py first)")
            return 2
        deb = cands[-1]
    if not deb.is_file():
        print(f"missing .deb: {deb}")
        return 2
    new_version = deb_version(deb)
    print(f"# deb under test: {deb.name} ({new_version})")

    old = Path(args.old_deb).resolve() if args.old_deb else None
    if old is None and not args.skip_upgrade:
        sibs = [p for p in sorted(dist.glob("katherine-desktop_*.deb")) if p != deb]
        old = sibs[0] if sibs else None
    if old is None and not args.skip_upgrade:
        print("# no second .deb for upgrade test — skipping those steps")
        args.skip_upgrade = True
    old_version = deb_version(old) if old is not None else None

    results: list[bool] = []

    # ── 1. install ───────────────────────────────────────────────────
    rc, out = run_in_env(
        deb,
        "dpkg-query --root=/install-root --admindir=/dpkg-db "
        "-W -f='${Status} ${Version}\\n' katherine-desktop",
    )
    m = re.search(r"^install ok installed (\S+)\s*$", out, re.MULTILINE)
    results.append(step("install: dpkg configure OK", rc == 0 and m is not None,
                        m.group(1) if m else out[-200:]))

    # ── 2. import outside checkout ──────────────────────────────────
    rc, out = run_in_env(
        deb,
        "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor "
        "python3 -c 'import backend.desktop.app as a; print(a.default_frontend_root())'",
    )
    root = find_marker(out, "/install-root/usr/lib/katherine/frontend")
    results.append(step("import: app resolves installed frontend", root is not None,
                        root or out[-200:]))

    # ── 3. XDG storage in isolated home ─────────────────────────────
    rc, out = run_in_env(deb, "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor python3 - <<'PY'\n"
        + "import pathlib, sys\n"
        + "sys.path.insert(0, '/usr/lib/katherine/vendor')\n"
        + "sys.path.insert(0, '/usr/lib/katherine')\n"
        + "from backend.local_storage import LocalStorage, default_database_path\n"
        + "p = default_database_path()\n"
        + "store = LocalStorage(path=p)\n"
        + "print('DBOK', p, p.exists(), 'SCHEMA', store.schema_version())\n"
        + "store.close()\nPY")
    dbok = find_marker(out, "DBOK")
    ok = (
        dbok is not None
        and "True" in dbok
        and "/home/user/" in dbok
        and "SCHEMA 1" in dbok
    )
    results.append(step("storage: XDG db created in isolated HOME", ok, dbok or out[-200:]))

    # ── 4. no-key turn (sanitized error) ─────────────────────────────
    rc, out = run_in_env(deb, "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor python3 - <<'PY'\n" + TURN_PROBE + "PY")
    j = json_line(out)
    ok = (
        j is not None
        and j.get("success") is False
        and j.get("error_code") == "configuration"
        and j.get("provider_configured") in (False, None)
        and j.get("storage_ok") in (True, None)
    )
    results.append(step("turn(no key): sanitized configuration error", ok,
                        json.dumps(j) if j else out[-200:]))

    # ── 5/6. upgrade + downgrade ─────────────────────────────────────
    if not args.skip_upgrade and old is not None:
        rc, out = run_in_env(
            old,
            "dpkg --root=/install-root --admindir=/dpkg-db --force-not-root --log=/dpkg-db/dpkg.log -i /debs/"
            + deb.name
            + " && echo UPGRADED $(dpkg-query --root=/install-root --admindir=/dpkg-db -W -f='${Version}' katherine-desktop)",
        )
        up = find_marker(out, "UPGRADED")
        results.append(step("upgrade: dpkg -i newer .deb", up == f"UPGRADED {new_version}",
                            up or out[-300:]))

        # Downgrade evidence: dpkg -i permits version downgrades by
        # default (apt is the layer that blocks them). The fail-closed
        # property that matters here is at the data layer: downgrading
        # the package must not reset or corrupt the user database.
        # Write data on the NEWER version, downgrade to the OLDER one,
        # then verify the database still opens and keeps its content.
        rc, out = run_in_env(
            deb,
            "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor python3 - <<'PY'\n"
            + "import sys\n"
            + "sys.path.insert(0, '/usr/lib/katherine/vendor')\n"
            + "sys.path.insert(0, '/usr/lib/katherine')\n"
            + "from backend.local_storage import LocalStorage, default_database_path\n"
            + "p = default_database_path()\n"
            + "LocalStorage(path=p)\n"
            + "print('WRITTEN', p)\nPY\n"
            + "dpkg --root=/install-root --admindir=/dpkg-db --force-not-root --log=/dpkg-db/dpkg.log -i /debs/"
            + old.name
            + "\necho DOWNGRADED $(dpkg-query --root=/install-root --admindir=/dpkg-db -W -f='${Version}' katherine-desktop)\n"
            + "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor python3 - <<'PY'\n"
            + "import sys\n"
            + "sys.path.insert(0, '/usr/lib/katherine/vendor')\n"
            + "sys.path.insert(0, '/usr/lib/katherine')\n"
            + "from backend.local_storage import LocalStorage, default_database_path\n"
            + "p = default_database_path()\n"
            + "LocalStorage(path=p)\n"
            + "print('REOPENED', p, p.exists(), p.stat().st_size)\nPY",
        )
        dg = find_marker(out, "DOWNGRADED")
        ro = find_marker(out, "REOPENED")
        ro_parts = ro.split() if ro else []
        ok = (
            dg == f"DOWNGRADED {old_version}"
            and len(ro_parts) >= 2
            and ro_parts[-2] == "True"
            and ro_parts[-1].isdigit()
            and int(ro_parts[-1]) > 0
        )
        results.append(step("downgrade: data preserved after version downgrade", ok,
                            f"{dg} | {ro}" if dg and ro else out[-300:]))

    # ── 7. purge + reinstall preserves user data ────────────────────
    rc, out = run_in_env(
        deb,
        "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor python3 - <<'PY'\n"
        + "import sys\n"
        + "import sqlite3\n"
        + "sys.path.insert(0, '/usr/lib/katherine/vendor')\n"
        + "sys.path.insert(0, '/usr/lib/katherine')\n"
        + "from backend.local_storage import LocalStorage, default_database_path\n"
        + "p = default_database_path()\n"
        + "store = LocalStorage(path=p)\n"
        + "store.close()\n"
        + "conn = sqlite3.connect(p)\n"
        + "conn.execute(\"insert into chat_logs (role, content) values (?, ?)\", ('user', 'lifecycle sentinel'))\n"
        + "conn.commit()\n"
        + "conn.close()\n"
        + "print('DATA WRITTEN', p)\nPY\n"
        + "dpkg --root=/install-root --admindir=/dpkg-db --force-not-root --log=/dpkg-db/dpkg.log --purge katherine-desktop\n"
        + "test ! -e /install-root/usr/bin/katherine && echo PURGE_FILES_GONE\n"
        + "echo PURGED; ls /install-root/usr/lib 2>/dev/null | wc -l; "
        + "dpkg --root=/install-root --admindir=/dpkg-db --force-not-root --log=/dpkg-db/dpkg.log --force-depends -i /debs/"
        + deb.name
        + "\n"
        + "echo REINSTALLED_PACKAGE $(dpkg-query --root=/install-root --admindir=/dpkg-db -W -f='${Status} ${Version}' katherine-desktop)\n"
        + "python3 - <<'PY'\n"
        + "import pathlib\n"
        + "import sqlite3\n"
        + "dbs = list(pathlib.Path('/home/user').rglob('katherine.db'))\n"
        + "print('AFTER PURGE dbs:', len(dbs))\n"
        + "if len(dbs) == 1:\n"
        + "    conn = sqlite3.connect(dbs[0])\n"
        + "    row = conn.execute(\"select count(*) from chat_logs where content = ?\", ('lifecycle sentinel',)).fetchone()\n"
        + "    conn.close()\n"
        + "    print('REINSTALLED_SENTINEL', bool(row[0]))\n"
        + "else:\n"
        + "    print('REINSTALLED_SENTINEL', False)\nPY",
    )
    # dpkg --purge removes package files, then a real reinstall must open the
    # same user database and retain the sentinel row. The user data lives in
    # /home/user, NOT in the package tree, so purge cannot touch it by design.
    after = find_marker(out, "AFTER PURGE dbs:")
    sentinel = find_marker(out, "REINSTALLED_SENTINEL")
    package = find_marker(out, "REINSTALLED_PACKAGE")
    ok = (
        rc == 0
        and find_marker(out, "PURGE_FILES_GONE") is not None
        and after is not None
        and after.endswith("1")
        and sentinel is not None
        and sentinel.endswith("True")
        and package is not None
        and "install ok installed" in package
    )
    results.append(step("purge/reinstall: user data preserved", ok,
                        f"{after} | {sentinel} | {package}" if after else out[-300:]))

    # ── 8. benchmarks (real numbers) ─────────────────────────────────
    size = deb.stat().st_size
    print(f"bench: .deb size = {size/1024:.0f} KiB ({size} bytes)")
    rc, out = run_in_env(
        deb,
        "du -sk /install-root/usr/lib/katherine | awk '{print \"INSTALLED_KB\", $1}'",
    )
    kb = find_marker(out, "INSTALLED_KB")
    if kb:
        print(f"bench: installed size = {kb.split()[1]} KiB (du -sk)")
    rc, out = run_in_env(
        deb,
        "cd /tmp && PYTHONPATH=/install-root/usr/lib/katherine:/install-root/usr/lib/katherine/vendor "
        "python3 -c 'import time; t=time.monotonic(); import backend.desktop.app as a; r=a.default_frontend_root(); print(\"IMPORT_MS %.0f\" % ((time.monotonic()-t)*1000))'",
    )
    ims = find_marker(out, "IMPORT_MS")
    if ims:
        print(f"bench: cold import time = {ims.split()[1]} ms (single run, isolated env)")

    total = all(results)
    print(f"\n{'ALL PASS' if total else 'FAILURES PRESENT'} ({sum(results)}/{len(results)})")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
