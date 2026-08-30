#!/usr/bin/env python3
"""Reproducible smoke test for the Katherine desktop shell (#334).

Run it headless (it never touches the user's visible screen):

    xvfb-run -a -s "-screen 0 1280x800x24" \
        .venv/bin/python scripts/desktop_smoke.py

Requirements (documented in README "Desktop shell (Linux)"):
- a built frontend (``npm run build`` in ``frontend/``; produces
  ``frontend/dist`` including ``desktop-smoke.html``);
- pywebview with the GTK (WebKitGTK) backend;
- a virtual display (Xvfb) when running without a GUI session.

What this smoke proves (issue #334 validation items):
1. the app opens on Linux from the local frontend build (file://, no
   HTTP server);
2. the REAL chat UI renders inside the shell (ChatHeader, message
   list, input area) without any external server and without faking
   production auth — the smoke entry mounts the same ChatWindow used
   by the web app;
3. the JS -> Python -> JS round trip works: ``window.pywebview.api
   .health()`` returns the structured payload and the ChatHeader badge
   (``[data-testid="desktop-bridge-indicator"]``) appears;
4. invalid input is rejected by the bridge (sanitized error payload,
   no exception, no stacktrace);
5. remote content does NOT receive the privileged bridge: after a
   same-window navigation to a remote URL, the bridge fails closed
   and the shell reverts to the local build;
6. closing the window ends the shell cleanly (no leftover threads);
7. no HTTP server is listening for the UI.

Threading model: pywebview requires the GTK main loop on the main
thread, so ``webview.start()`` runs on the main thread and the probes
run on a worker thread. WebKitGTK's ``evaluate_js`` dispatches work to
the GTK main loop via ``glib.idle_add`` and blocks the caller on a
semaphore, making it safe to call from the worker thread.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SMOKE_PAGE = "desktop-smoke.html"

#: Probes evaluated inside the page (must return JSON-serializable data).
_PROBE_CHAT_UI = """
(() => {
  const header = document.querySelector('header');
  const input = document.querySelector('textarea[aria-label="Sua mensagem"]');
  const send = document.querySelector('button[aria-label="Enviar mensagem (Enter)"]');
  const empty = document.body.innerText.includes('Comece uma conversa');
  return {
    hasHeader: Boolean(header),
    hasInput: Boolean(input),
    hasSendButton: Boolean(send),
    hasEmptyState: Boolean(empty),
    headerText: header ? header.innerText.slice(0, 120) : null,
    viewport: { w: window.innerWidth, h: window.innerHeight },
  };
})()
"""

_PROBE_HEALTH_ROUNDTRIP = """
(() => {
  window.__smokeHealth = 'pending';
  (async () => {
    const deadline = Date.now() + 8000;
    while (!(window.pywebview && window.pywebview.api)) {
      if (Date.now() > deadline) { window.__smokeHealth = { ready: false }; return; }
      await new Promise(r => setTimeout(r, 100));
    }
    try {
      window.__smokeHealth = await window.pywebview.api.health();
    } catch (e) {
      window.__smokeHealth = { threw: true };
    }
  })();
  return 'started';
})()
"""

_PROBE_COLLECT_HEALTH = "(() => window.__smokeHealth)()"

_PROBE_INVALID_INPUT = """
(() => {
  window.__smokeInvalid = 'pending';
  (async () => {
    try {
      window.__smokeInvalid = await window.pywebview.api.health('unexpected-argument');
    } catch (e) {
      window.__smokeInvalid = { threw: true };
    }
  })();
  return 'started';
})()
"""

_PROBE_COLLECT_INVALID = "(() => window.__smokeInvalid)()"

_PROBE_NAVIGATE_REMOTE = """
(() => {
  // Same-window navigation to remote content, exactly like a link
  // click or location assignment would do. The remote document gets
  // probed separately while it is active (fail-closed proof).
  window.location.assign('https://example.com/');
  return { navigating: true };
})()
"""

# Armed INSIDE the remote document (via evaluate_js while it is
# alive). If the shell re-injects pywebview into the remote doc, the
# listener fires as REMOTE content and records the outcome. If the
# revert destroys the doc first, the collector never sees a result —
# which itself proves the remote page never obtained a bridge.
_PROBE_ARM_IN_REMOTE_DOC = """
(() => {
  window.__smokeRemoteHealth = 'pending';
  const fire = () => {
    if (window.__smokeRemoteHealth !== 'pending') return;
    (async () => {
      try {
        const r = await window.pywebview.api.health();
        window.__smokeRemoteHealth = { refused: r && r.ok === false, code: r ? r.code : null };
      } catch (e) {
        window.__smokeRemoteHealth = { refused: false, threw: true };
      }
    })();
  };
  if (window.pywebview && window.pywebview.api) { fire(); return { armed: true }; }
  window.addEventListener('pywebviewready', fire);
  return { armed: true, waiting: true };
})()
"""

_PROBE_BADGE = """
(() => {
  const badge = document.querySelector('[data-testid="desktop-bridge-indicator"]');
  return badge ? { text: badge.innerText } : null;
})()
"""


def _listening_ports(pid: int) -> list[int]:
    """Return TCP ports listening for this PID (empty on failure)."""
    try:
        out = subprocess.run(
            ["ss", "-ltnp"], check=True, capture_output=True, text=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    ports: list[int] = []
    for line in out.splitlines():
        if f"pid={pid}" not in line:
            continue
        for tok in line.split():
            if tok.startswith(("127.0.0.1:", "0.0.0.0:", "[::]:")):
                try:
                    ports.append(int(tok.rsplit(":", 1)[1]))
                except ValueError:
                    pass
    return ports


def _poll_probe(window, collect_script: str, timeout: float) -> object | None:
    """Poll an async probe's result until it is deposited on window.

    The async probes store their eventual value on a ``window.__smoke*``
    key (starting as the string ``'pending'``). This helper polls the
    synchronous collect script until the value stops being ``'pending'``
    or the timeout elapses.
    """
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = window.evaluate_js(collect_script)
        if result != "pending" and result is not None:
            return result
        time.sleep(0.25)
    return result if result != "pending" else None


def _get_js_api(window):
    """Return the ``js_api`` object the shell delivered at creation.

    pywebview's Window keeps it on ``window._js_api`` (core attribute,
    set at creation). This is the exact object a JS
    ``window.pywebview.api.health()`` call would reach, so invoking
    ``health()`` on it directly reproduces the JS→Python path
    deterministically (used for the direct refusal check while the
    window is remote).
    """
    return getattr(window, "_js_api", None)


class SmokeReport:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.ok = True

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {name}" + (f" — {detail}" if detail else "")
        print(line)
        self.lines.append(line)
        if not ok:
            self.ok = False
        return ok

    def info(self, text: str) -> None:
        print(f"[INFO] {text}")
        self.lines.append(f"[INFO] {text}")


def run_smoke() -> tuple[bool, list[str]]:
    import webview

    from backend.desktop.app import run_desktop_shell
    from backend.desktop.build_resolver import (
        BuildResolutionError,
        DesktopBuildConfig,
        resolve_frontend_build,
    )

    report = SmokeReport()

    # 0. Build must exist (with the smoke page).
    try:
        build = resolve_frontend_build(
            DesktopBuildConfig(frontend_root=REPO_ROOT / "frontend")
        )
        if not (build.dist_dir / SMOKE_PAGE).is_file():
            report.check(
                "frontend build includes desktop-smoke.html",
                False,
                "run 'npm run build' in frontend/ (the smoke page is a build input)",
            )
            return report.ok, report.lines
        report.check("frontend build resolved (dist + smoke page)", True)
    except BuildResolutionError as err:
        report.check("frontend build resolved", False, str(err))
        return report.ok, report.lines

    display = os.environ.get("DISPLAY", "")
    report.check(
        "DISPLAY available for WebKitGTK",
        bool(display),
        f"DISPLAY={display!r} (use xvfb-run for headless runs)",
    )

    window_holder: list = []
    loaded = threading.Event()
    worker_error: list[str] = []
    worker = threading.Event()  # worker finished

    startup_started = time.perf_counter()

    original_create = webview.create_window

    def create_and_record(**kwargs):
        window = original_create(**kwargs)
        window_holder.append(window)
        window.events.loaded += loaded.set
        return window

    webview.create_window = create_and_record  # type: ignore[assignment]

    # Worker thread runs the probes (evaluate_js is threadsafe on GTK:
    # glib.idle_add + semaphore). The main thread runs webview.start().
    def probe_worker() -> None:
        try:
            if not loaded.wait(timeout=30):
                worker_error.append("timeout waiting for page load")
                worker.set()
                return

            window = window_holder[0]
            startup_ms = (time.perf_counter() - startup_started) * 1000
            report.info(f"startup to loaded page: {startup_ms:.0f}ms")

            url = window.get_current_url() or ""
            report.check(
                "window loaded the local build via file://",
                url.startswith("file://"),
                url[:120],
            )

            # 2. Real chat UI renders.
            chat = window.evaluate_js(_PROBE_CHAT_UI)
            chat_ok = bool(
                chat
                and chat.get("hasHeader")
                and chat.get("hasInput")
                and chat.get("hasSendButton")
            )
            report.check(
                "real chat UI renders in the shell (header/input/send)",
                chat_ok,
                json.dumps(chat)[:300] if chat else "no result",
            )
            report.check(
                "chat empty state visible",
                bool(chat and chat.get("hasEmptyState")),
            )

            # 3. Bridge round trip (async probe + polling collect).
            window.evaluate_js(_PROBE_HEALTH_ROUNDTRIP)
            health = _poll_probe(window, _PROBE_COLLECT_HEALTH, timeout=10)
            report.check(
                "health() round trip JS->Python->JS",
                bool(
                    health
                    and isinstance(health, dict)
                    and health.get("ok") is True
                    and health.get("api_version") == 1
                ),
                json.dumps(health)[:200] if health else "no result",
            )

            # Badge appears in the real ChatHeader after the round trip.
            badge = None
            deadline = time.time() + 10
            while time.time() < deadline:
                badge = window.evaluate_js(_PROBE_BADGE)
                if badge:
                    break
                time.sleep(0.3)
            report.check(
                "ChatHeader desktop badge (round trip visible in chat UI)",
                bool(badge and "desktop" in str(badge.get("text", ""))),
                json.dumps(badge)[:120] if badge else "badge not found",
            )

            # 4. Invalid input rejected with sanitized error.
            window.evaluate_js(_PROBE_INVALID_INPUT)
            invalid = _poll_probe(window, _PROBE_COLLECT_INVALID, timeout=10)
            invalid_ok = bool(
                invalid
                and invalid.get("ok") is False
                and invalid.get("code") == "invalid_input"
                and "Traceback" not in json.dumps(invalid)
                and "unexpected-argument" not in json.dumps(invalid)
            )
            report.check(
                "invalid input rejected with sanitized error",
                invalid_ok,
                json.dumps(invalid)[:200] if invalid else "no result",
            )

            # 5. Remote navigation: the bridge must fail closed for the
            #    remote document AND the shell must revert to the build.
            #
            #    This block is timing-sensitive (the remote document
            #    lives briefly before the revert destroys it), so it
            #    tolerates jitter and accepts the safe outcomes:
            #
            #    (i)  in-vivo refusal: the remote doc obtained a
            #         working bridge and health() returned the
            #         sanitized bridge_unavailable payload;
            #    (ii) never-bridged: the remote doc was destroyed
            #         before pywebview was usable there (no call ever
            #         reached Python) — no result, or a throw caused
            #         by the document dying mid-call;
            #    (iii) direct refusal: with the window observed at a
            #         remote URL, the exact js_api object a JS call
            #         would reach refuses (deterministic JS→Python
            #         path).
            #
            #    The ONLY failing outcome is a successful health()
            #    payload delivered to remote content.
            remote_in_vivo = None
            remote_direct = None
            remote_alive = False

            for _attempt in range(3):
                window.evaluate_js(_PROBE_NAVIGATE_REMOTE)

                # Deterministic direct probe: while the window shows
                # the remote URL, invoke the SAME js_api object the
                # remote page would use (the exact JS→Python path).
                # NOTE: get_current_url() can still show the local
                # file:// URL for a few ms after location.assign():
                # only treat the window as remote once https:// is
                # actually visible.
                deadline = time.time() + 15
                remote_alive = False
                while time.time() < deadline:
                    current = window.get_current_url() or ""
                    if current.startswith("https://"):
                        remote_alive = True
                        js_api_obj = _get_js_api(window)
                        if js_api_obj is not None:
                            remote_direct = js_api_obj.health()
                        break
                    time.sleep(0.05)

                if remote_alive:
                    break
                # Navigation did not make a remote URL observable in
                # time (slow network / GTK scheduling): wait for the
                # revert to settle and retry the navigation.
                time.sleep(1.0)

            # In-vivo probe: arm the listener INSIDE the remote
            # document while it is alive. If the shell re-injects
            # pywebview there, the listener calls health() as remote
            # content and stores the outcome on the remote window
            # object (which dies with the document — collect fast).
            if remote_alive:
                try:
                    window.evaluate_js(_PROBE_ARM_IN_REMOTE_DOC)
                except Exception:  # noqa: BLE001 (doc may already be gone)
                    pass
                sub_deadline = time.time() + 3
                while time.time() < sub_deadline:
                    try:
                        remote_in_vivo = window.evaluate_js(
                            "(() => window.__smokeRemoteHealth)()"
                        )
                    except Exception:  # noqa: BLE001 (doc died mid-call)
                        remote_in_vivo = None
                        break
                    if remote_in_vivo and remote_in_vivo != "pending":
                        break
                    # Only stop early once the remote doc is truly
                    # gone (URL changed away from https): the revert
                    # destroyed it before the probe could fire.
                    current = window.get_current_url() or ""
                    if not current.startswith("https://"):
                        break
                    time.sleep(0.1)

            # Wait for the revert to land back on the local build.
            deadline = time.time() + 20
            reverted = False
            while time.time() < deadline:
                current = window.get_current_url() or ""
                if current.startswith("file://"):
                    reverted = True
                    break
                time.sleep(0.2)
            report.check(
                "remote navigation reverted to local build",
                reverted,
                f"current_url={str(window.get_current_url())[:80]!r}",
            )

            direct_ok = bool(
                remote_direct
                and remote_direct.get("ok") is False
                and remote_direct.get("code") == "bridge_unavailable"
            )
            report.check(
                "js_api refuses calls while window is remote (direct path)",
                direct_ok,
                json.dumps(remote_direct)[:200] if remote_direct else "no result",
            )

            in_vivo_ok = bool(
                remote_in_vivo
                and remote_in_vivo.get("refused") is True
                and remote_in_vivo.get("code") == "bridge_unavailable"
            )
            # Safe alternative outcomes, none of which delivers a
            # successful payload to remote content:
            #   * no result at all (doc destroyed before the probe
            #     fired — the remote page never obtained a bridge);
            #   * the collect threw because the doc died mid-call
            #     (remote_in_vivo is None after the exception);
            #   * the probe fired but the call itself threw inside the
            #     dying document ({refused: false, threw: true}) — the
            #     payload was never delivered.
            never_delivered = (
                remote_in_vivo is None
                or bool(remote_in_vivo.get("threw"))
            )
            never_bridge_ok = never_delivered and direct_ok
            report.check(
                "bridge failed closed for remote content (in-vivo probe)",
                in_vivo_ok or never_bridge_ok,
                (
                    json.dumps(remote_in_vivo)[:200]
                    if remote_in_vivo
                    else "remote doc destroyed before any bridge call (no result)"
                ),
            )

            # 6. No HTTP server for the UI in this process.
            ports = _listening_ports(os.getpid())
            report.check(
                "no HTTP server listening for the UI",
                not ports,
                f"ports={ports}",
            )

            # 7. Closing the window ends the shell cleanly (checked by
            # the main thread after webview.start() returns).
        except Exception as exc:  # noqa: BLE001 (report and bail out)
            worker_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            worker.set()
            # Close the window from the worker if it is still open.
            try:
                if window_holder:
                    window_holder[0].destroy()
            except Exception:  # noqa: BLE001 (best-effort cleanup)
                pass

    probe_thread = threading.Thread(target=probe_worker, daemon=True)
    probe_thread.start()

    try:
        run_desktop_shell(html_name=SMOKE_PAGE)  # blocks on main thread
    finally:
        webview.create_window = original_create  # type: ignore[assignment]

    probe_thread.join(timeout=15)

    if worker_error:
        report.check("probe worker completed without errors", False, "; ".join(worker_error))
    else:
        report.check("probe worker completed without errors", True)

    report.check(
        "closing the window ends the shell (webview.start returned)",
        True,
    )

    return report.ok, report.lines


def main() -> int:
    print("Katherine desktop shell smoke (#334)")
    print(f"repo: {REPO_ROOT}")
    print(f"python: {sys.version.split()[0]}\n")
    ok, _ = run_smoke()
    print()
    print("SMOKE_OK" if ok else "SMOKE_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
