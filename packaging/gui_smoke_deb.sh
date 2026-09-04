#!/bin/bash
# Katherine packaged-app GUI smoke (#338).
#
# Runs the INSTALLED /usr/bin/katherine entrypoint (production
# desktop.html, not the smoke page) inside the isolated install
# environment, on a virtual X display, and proves:
#
#   1. the packaged app launches its GTK window (WebKitGTK alive);
#   2. no network listener is opened for the UI (file:// build only);
#   3. closing the app leaves ZERO katherine processes behind;
#   4. the XDG storage is created by the app itself on first run.
#
# This complements packaging/smoke_deb.py (dpkg lifecycle) with the
# GUI acceptance path of the installed package.
#
# Usage:
#   packaging/gui_smoke_deb.sh <deb>
# Requires: Xvfb and xdotool on the host; the isolated-install harness.
# openbox is optional and only supplies normal WM close semantics.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEB="$(readlink -f "${1:?usage: gui_smoke_deb.sh <deb>}")"

command -v xvfb-run >/dev/null 2>&1 || {
  echo "error: xvfb-run is required for GUI smoke" >&2
  exit 2
}
command -v xdotool >/dev/null 2>&1 || {
  echo "error: xdotool is required for GUI input smoke" >&2
  exit 2
}

# ── probe (runs INSIDE the isolated env, after dpkg install) ─────
PROBE='
import json, os, pathlib, subprocess, sys, time

def wait_for(fn, timeout=20, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None

results = {}

def read_cmdline(pid):
    try:
        return pathlib.Path("/proc", str(pid), "cmdline").read_bytes().replace(b"\\0", b" ").decode(errors="replace").strip()
    except (FileNotFoundError, PermissionError):
        return ""

def read_ppid(pid):
    try:
        for line in pathlib.Path("/proc", str(pid), "status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return None

def read_pgid(pid):
    try:
        raw = pathlib.Path("/proc", str(pid), "stat").read_text()
        # Fields after the final parenthesized command are state, ppid, pgrp, ...
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[2])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None

def descendants(root_pid):
    known = {root_pid}
    changed = True
    while changed:
        changed = False
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in known:
                continue
            if read_ppid(pid) in known:
                known.add(pid)
                changed = True
    return known

def process_group_pids(pgid):
    pids = set()
    for entry in os.listdir("/proc"):
        if entry.isdigit() and read_pgid(int(entry)) == pgid:
            pids.add(int(entry))
    return pids

def rss_kib(pid):
    try:
        for line in pathlib.Path("/proc", str(pid), "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        return 0
    return 0

def cpu_jiffies(pid):
    try:
        raw = pathlib.Path("/proc", str(pid), "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[11]) + int(fields[12])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return 0

def total_rss(pids):
    return sum(rss_kib(pid) for pid in pids)

def total_cpu(pids):
    return sum(cpu_jiffies(pid) for pid in pids)

def cpu_percent(pids, duration=2.0):
    start_cpu = total_cpu(pids)
    started = time.monotonic()
    time.sleep(duration)
    elapsed = time.monotonic() - started
    ticks = os.sysconf("SC_CLK_TCK")
    return round((total_cpu(pids) - start_cpu) / ticks / elapsed * 100, 2)

# Launch the installed entrypoint as a subprocess: its process
# lifecycle (leftovers, exit code) is exactly what a user sees.
# NOTE: inside the isolated env the package tree lives under
# /install-root; the entrypoint path below is the installed one.
launch_started = time.monotonic()
proc = subprocess.Popen(
    ["/install-root/usr/bin/katherine"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    start_new_session=True,
)
app_pgid = read_pgid(proc.pid) or proc.pid

# GTK window appears on the display (WebKitGTK creates one).
def window_up():
    if os.path.exists("/usr/bin/xdotool"):
        r = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "Katherine"],
            capture_output=True, text=True)
        ids = r.stdout.splitlines()
        return ids[0] if r.returncode == 0 and ids else False
    # Fallback without xdotool: the app is alive and consuming CPU
    # (the GTK main loop runs) — weaker, but still a real process.
    try:
        with open(f"/proc/{proc.pid}/stat") as f:
            parts = f.read().split()
        state = parts[2]
        return state in ("R", "S")
    except FileNotFoundError:
        return False

window_id = wait_for(window_up)
results["gtk_window_up"] = bool(window_id)
results["startup_ms"] = round((time.monotonic() - launch_started) * 1000, 1)

# Storage: the app itself must create the XDG db on first run.
home = pathlib.Path(os.environ.get("HOME", "/home/user"))
def db_created():
    return (home / ".local/share/katherine/katherine.db").exists()
results["xdg_db_created"] = bool(wait_for(db_created, timeout=10))

# Snapshot the complete process subtree while the app is alive.  The
# WebKit network/render processes are children of the installed shell;
# they must disappear with it too.
observed_pids = descendants(proc.pid)
observed_pids.update(process_group_pids(app_pgid))

# Let the first WebKit load settle before taking idle measurements.
time.sleep(1.0)
results["idle_rss_kib"] = total_rss(observed_pids)
results["idle_cpu_percent"] = cpu_percent(observed_pids, duration=5.0)

# Drive one offline turn through the real UI.  No provider key is
# configured by the harness, so this exercises the bridge and local
# error path without network access or provider quota.
turn_sent = False
if window_id and os.path.exists("/usr/bin/xdotool"):
    commands = (
        ["/usr/bin/xdotool", "windowfocus", "--sync", str(window_id)],
        ["/usr/bin/xdotool", "windowactivate", "--sync", str(window_id)],
        # xdotool coordinates are relative to the client window.  The
        # message field is on the left chat column in the wide desktop
        # layout, around x=350 and y=700 on the 1280x800 smoke display.
        ["/usr/bin/xdotool", "mousemove", "--window", str(window_id), "350", "700"],
        ["/usr/bin/xdotool", "click", "1"],
        ["/usr/bin/xdotool", "type", "--clearmodifiers", "packaged smoke no key"],
        ["/usr/bin/xdotool", "key", "--clearmodifiers", "Return"],
    )
    turn_sent = True
    for command in commands:
        if subprocess.run(command, capture_output=True).returncode != 0:
            turn_sent = False
            break
        if command[1] == "click":
            # Let WebKitGTK deliver the focus event before synthetic text.
            time.sleep(0.25)
results["ui_turn_sent"] = turn_sent
if turn_sent:
    time.sleep(0.5)
    results["turn_rss_peak_kib"] = 0
    turn_started = time.monotonic()
    turn_cpu_start = total_cpu(observed_pids)
    while time.monotonic() - turn_started < 3.0:
        observed_pids.update(descendants(proc.pid))
        results["turn_rss_peak_kib"] = max(
            results["turn_rss_peak_kib"], total_rss(observed_pids)
        )
        time.sleep(0.1)
    ticks = os.sysconf("SC_CLK_TCK")
    results["turn_cpu_percent"] = round(
        (total_cpu(observed_pids) - turn_cpu_start)
        / ticks / max(time.monotonic() - turn_started, 0.001)
        * 100,
        2,
    )

# No UI TCP listener: file:// build only, no server, no daemon.
observed_pids.update(descendants(proc.pid))
observed_pids.update(process_group_pids(app_pgid))
def my_listeners():
    listeners = []
    for pid in observed_pids:
        try:
            socket_inodes = set()
            for fd in pathlib.Path(f"/proc/{pid}/fd").iterdir():
                try:
                    link = os.readlink(fd)
                except (FileNotFoundError, PermissionError):
                    continue
                if link.startswith("socket:[") and link.endswith("]"):
                    socket_inodes.add(link[8:-1])

            for proto in ("tcp", "tcp6"):
                path = pathlib.Path(f"/proc/{pid}/net/{proto}")
                for line in path.read_text().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) > 9 and fields[3] == "0A" and fields[9] in socket_inodes:
                        listeners.append(f"pid={pid} {line.strip()}")
        except (FileNotFoundError, PermissionError):
            continue
    return listeners
lis = my_listeners()
results["ui_tcp_listeners"] = lis if lis is not None else "proc-unavailable"

# Close the actual GTK window (the user/session-close path), not just
# the process.  This makes run_desktop_shell return and execute its
# runtime.close() finally block.  A no-xdotool fallback still uses
# SIGTERM so the check remains useful on minimal CI images.
closed_by_window = False
if window_id and os.path.exists("/usr/bin/xdotool"):
    closed_by_window = subprocess.run(
        ["/usr/bin/xdotool", "windowclose", str(window_id)],
        capture_output=True,
    ).returncode == 0
if not closed_by_window:
    proc.terminate()
shutdown_started = time.monotonic()
try:
    rc = proc.wait(timeout=10)
    results["clean_exit"] = (rc == 0) if closed_by_window else (rc is not None)
    results["exit_code"] = rc
    results["shutdown_ms"] = round((time.monotonic() - shutdown_started) * 1000, 1)
    if proc.stdout is not None:
        output = proc.stdout.read()[-2000:]
        results["no_key_turn_observed"] = (
            "event=local_turn_failed code=configuration" in output
        )
        if rc != 0 and output.strip():
            results["app_output"] = output
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=5)
    results["clean_exit"] = False
    results["exit_code"] = "timeout"
    results["no_key_turn_observed"] = False

time.sleep(0.5)
# Re-scan both the live subtree and the dedicated process group after the
# shell exits. The group catches WebKit descendants that reparent before the
# final check, while start_new_session keeps the probe and harness out of it.
leftovers = []
for _ in range(20):
    observed_pids.update(process_group_pids(app_pgid))
    leftovers = []
    for pid in sorted(observed_pids):
        if pid == os.getpid():
            continue
        cmdline = read_cmdline(pid)
        if cmdline:
            leftovers.append(f"{pid} {cmdline}")
    if not leftovers:
        break
    time.sleep(0.25)
results["leftover_processes"] = " | ".join(leftovers)

print("GUI_RESULTS " + json.dumps(results))
ok = (
    results["gtk_window_up"] is True
    and results["xdg_db_created"] is True
    and results["ui_turn_sent"] is True
    and results["no_key_turn_observed"] is True
    and results["ui_tcp_listeners"] in ([], "proc-unavailable")
    and results["clean_exit"] is True
    and results["leftover_processes"] == ""
)
print("GUI_SMOKE_OK" if ok else "GUI_SMOKE_FAILED")
sys.exit(0 if ok else 1)
'

# Allocate a fresh display and let xvfb-run clean it up.  The inner
# shell copies xvfb-run's dynamically selected DISPLAY into KAT_DISPLAY
# before the harness pivots its root.  -ac plus /dev/null avoids needing
# to expose the host's Xauthority file (which is outside the isolated
# HOME by design).
exec xvfb-run -a -s "-screen 0 1280x800x24 -ac" bash -c '
  # A WM makes WM_DELETE_WINDOW behave like a normal desktop close;
  # without one, Xvfb can report a harmless asynchronous BadDrawable.
  if command -v openbox >/dev/null 2>&1; then
    openbox >/dev/null 2>&1 & WM_PID=$!
    trap "kill $WM_PID 2>/dev/null || true" EXIT
    sleep 1
  fi
  KAT_DISPLAY="$DISPLAY" XAUTHORITY=/dev/null bash "$1" "$2" "$3"
' _ "$HERE/isolated-install.sh" "$DEB" "python3 -c '
$PROBE
'"
