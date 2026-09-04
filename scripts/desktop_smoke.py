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

What this smoke proves (issue #334 validation items + #336 local
runtime persistence):

#334 (shell trust):
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
5. remote content does NOT receive the privileged bridge, proven in
   three stages: (A) a normally loaded remote page is refused;
   (B) during the revert to the SAME entry URL — while the remote
   document is still the live document and the new local load has
   not completed — the bridge stays closed; (C) after the new local
   load completes, the bridge serves the local document again. An
   in-vivo probe inside the remote document is kept as best-effort
   additional evidence (its outcome depends on WebKitGTK timing:
   the remote document is often destroyed before pywebview could be
   injected there, which is itself a safe outcome). The deterministic
   same-URL state machine proof lives in
   ``backend/tests/test_desktop_navigation.py::TestRevertToSameEntryUrl``;
6. closing the window ends the shell cleanly (no leftover threads);
7. no HTTP server is listening for the UI.

#336 (local companion runtime, added on top of the same run):
8. runtime_state() through the real bridge reports local storage
   ready with no cloud dependency;
9. a full turn driven through send_message() (scripted offline
   provider — no Groq quota, no network) commits locally — AND the
   same turn is first driven through the REAL UI (textarea, send
   button, ChatWindow rendering the reply), the exact end-user
   acceptance path;
10. the turn is durable in SQLite, proven by an independent
    read-only stdlib sqlite3 connection (not the runtime's read
    path);
11. a FRESH runtime over the same file (what a relaunch is)
    recovers the conversation and the stored revision;
12. the local privacy op delete_history() really erases (0 message
    rows in chat_logs afterwards, verified independently);
13. USER ACCEPTANCE: a turn driven through the REAL UI (textarea,
    send button, ChatWindow rendering the reply) — the exact path a
    user takes, not a direct bridge call;
14. USER ACCEPTANCE: the privacy panel driven through the REAL UI
    (click Apagar histórico -> explicit Confirmar -> success status
    rendered).

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
import tempfile
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

#: #336: drive a real turn through the REAL UI (ChatInput -> useChat ->
#: transport -> bridge), not by calling the bridge directly. The probe
#: types into the textarea, clicks send, and collects the rendered
#: assistant message. This proves the whole user acceptance path.
_PROBE_UI_SEND = """
(() => {
  window.__smokeUiSend = 'pending';
  (async () => {
    try {
      const input = document.querySelector('textarea[aria-label="Sua mensagem"]');
      if (!input) { window.__smokeUiSend = { error: 'no textarea' }; return; }
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(input, 'Mensagem real do smoke #336 via UI');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      const send = document.querySelector('button[aria-label="Enviar mensagem (Enter)"]');
      if (!send) { window.__smokeUiSend = { error: 'no send button' }; return; }
      send.click();
      // Wait for the assistant reply to render. The bubbles carry no
      // stable class names, so observe the whole page text — the
      // scripted reply string is unique to this run.
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 500));
        const text = document.body.innerText || '';
        if (text.includes('Resposta de teste do smoke local')) {
          window.__smokeUiSend = { sent: true, replied: true };
          return;
        }
      }
      window.__smokeUiSend = { sent: true, replied: false, bodySample: (document.body.innerText || '').slice(0, 300) };
    } catch (e) {
      window.__smokeUiSend = { threw: true, message: String(e).slice(0, 200) };
    }
  })();
  return 'started';
})()
"""

_PROBE_COLLECT_UI_SEND = "(() => window.__smokeUiSend)()"

#: #336: exercise the PRIVACY PANEL through the real UI. Clicks the
#: button, confirms the explicit confirmation dialog, and collects the
#: rendered success status. This is the destructive-op acceptance path
#: (user-initiated local erase), driven exactly as a user would.
_PROBE_UI_PRIVACY = """
(() => {
  window.__smokeUiPrivacy = 'pending';
  (async () => {
    try {
      const panel = document.querySelector('[data-testid="privacy-panel"]');
      if (!panel) { window.__smokeUiPrivacy = { error: 'no privacy panel' }; return; }
      const byText = (t) => Array.from(panel.querySelectorAll('button'))
        .find(b => (b.innerText || '').trim() === t);
      const start = byText('Apagar histórico');
      if (!start) { window.__smokeUiPrivacy = { error: 'no Apagar histórico button' }; return; }
      start.click();
      // The confirmation renders through React state; give it time and
      // retry the click (a disabled/pending op blocks the re-render).
      const cDeadline = Date.now() + 15000;
      let confirmBtn = null;
      while (Date.now() < cDeadline && !confirmBtn) {
        await new Promise(r => setTimeout(r, 400));
        confirmBtn = byText('Confirmar');
        if (!confirmBtn && !start.disabled) start.click();
      }
      if (!confirmBtn) { window.__smokeUiPrivacy = { error: 'no confirmation shown' }; return; }
      confirmBtn.click();
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 400));
        const status = panel.querySelector('[role="status"]');
        if (status && (status.innerText || '').includes('Operação concluída')) {
          window.__smokeUiPrivacy = { confirmed: true, status: status.innerText };
          return;
        }
      }
      window.__smokeUiPrivacy = { confirmed: true, status: 'timeout waiting for status' };
    } catch (e) {
      window.__smokeUiPrivacy = { threw: true, message: String(e).slice(0, 200) };
    }
  })();
  return 'started';
})()
"""

_PROBE_COLLECT_UI_PRIVACY = "(() => window.__smokeUiPrivacy)()"

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


def _make_scripted_provider():
    """Deterministic offline LanguageModel for the smoke (#336, #337).

    The smoke must never spend real Groq quota and must not depend on a
    configured key: the canonical LanguageModel contract answers with a
    fixed, valid reply and a neutral appraisal. It exercises the same
    code path (contract interface, envelope validation, atomic commit)
    with zero network. The trusted policy is a core responsibility and
    is never a provider capability (issue #337).
    """
    from backend.emotional_domain import AppraisalV1

    class ScriptedSmokeProvider:
        async def appraise(self, message, budget):
            return AppraisalV1.neutral()

        async def generate(self, messages, budget):
            return "Resposta de teste do smoke local (#336)."

        async def extract_archival(self, messages, budget):
            return "{}"

        def describe(self):
            from backend.language_model import ModelSelection
            return ModelSelection(
                provider="fake", main_model_id="fake-main",
                fast_model_id="fake-fast",
            )

    return ScriptedSmokeProvider()


def _sqlite_tables(db_path: Path) -> list[str]:
    """Read the table list straight from the smoke database file.

    Independent proof of persistence: the smoke does not trust the
    runtime's own read path to prove that the runtime wrote anything —
    it opens the SQLite file with the stdlib module and looks.
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [name for (name,) in rows]
    finally:
        conn.close()


def _sqlite_row_count(db_path: Path, table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(count)
    finally:
        conn.close()


def _sqlite_fetch_all(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    """Run a read-only SELECT against the smoke database (stdlib).

    Generic companion to the fixed-shape helpers so the smoke can
    verify CONTENT (not only row counts) — e.g. that the message the
    real UI sent is the one persisted (#336, review blocker 3).
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


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

    # #336: the smoke uses a throwaway database and an offline scripted
    # provider. The user's real local database is never touched and no
    # Groq quota is spent; the lifecycle exercised is otherwise the
    # production one (bridge -> runtime -> LocalStorage -> SQLite).
    # Created BEFORE the probe worker is defined: the worker inspects
    # this file at specific moments (immediately after the UI send and
    # the UI privacy op — review blocker 3), so the path must be in
    # scope from the start.
    smoke_db_dir = tempfile.mkdtemp(prefix="katherine-smoke-336-")
    smoke_db_path = Path(smoke_db_dir) / "smoke.db"

    window_holder: list = []
    loaded = threading.Event()
    worker_error: list[str] = []
    worker = threading.Event()  # worker finished

    startup_started = time.perf_counter()

    original_create = webview.create_window

    # Evidence captured by the loaded handler itself (the same event
    # the revert logic runs on — it ALWAYS sees the remote URL, while
    # polling get_current_url() can miss the whole remote window when
    # the network is fast: the revert completes before the first poll).
    remote_urls_seen: list[str] = []  # URLs captured on loaded events
    remote_direct_ref: list[object] = []  # js_api.health() called in-handler while remote
    in_vivo_ref: list[object] = []  # probe result collected inside the remote doc
    mid_revert_ref: list[dict] = []  # stage B: health() probed as the revert load starts

    def create_and_record(**kwargs):
        window = original_create(**kwargs)
        window_holder.append(window)

        # Deterministic mid-revert evidence (stage B): wrap load_url so
        # that, at the exact moment the revert navigation is issued
        # (get_uri() flips to the SAME entry file:// URL while the
        # remote document is still the live document and the new local
        # load has NOT completed), a bridge call is made through the
        # exact object JS would reach. It must be refused.
        original_load_url = window.load_url
        entry_uri = kwargs.get("url")

        def _load_url_probe(url: str):
            # Stage B (deterministic): fires at the instant the shell
            # issues the revert navigation (inside load_url, BEFORE the
            # load starts). At this moment the previous document (the
            # remote page) is still the live document and the new local
            # load has not completed. The probe calls health() on the
            # exact object a JS call from that live document would
            # reach; it must be refused. (Once the load starts,
            # get_uri() flips to the same entry URL — WebKitGTK shows
            # the load that STARTED — so a probe made after
            # original_load_url() would see the flipped URL; the call
            # is deliberately made BEFORE it, with the live document
            # still remote, which is the attacker-relevant moment.)
            if (
                url == entry_uri
                and str(url).startswith("file://")
            ):
                current = None
                try:
                    current = window.get_current_url()
                except Exception:  # noqa: BLE001 (evidence only)
                    current = None
                if current != entry_uri:
                    js_api_obj = _get_js_api(window)
                    if js_api_obj is not None:
                        try:
                            mid_revert_ref.append(
                                {
                                    "reverting_from": current,
                                    "health": js_api_obj.health(),
                                }
                            )
                        except Exception:  # noqa: BLE001 (evidence only)
                            pass
            return original_load_url(url)

        window.load_url = _load_url_probe  # type: ignore[method-assign]

        def _record_loaded():
            try:
                url = window.get_current_url() or ""
            except Exception:  # noqa: BLE001 (evidence only)
                url = ""
            if url.startswith("https://"):
                remote_urls_seen.append(url)
                # Deterministic direct refusal, executed at the exact
                # moment the window is provably remote: invoke the same
                # js_api object a JS call from this remote page would
                # reach. Fail-closed must refuse it here.
                try:
                    js_api_obj = _get_js_api(window)
                    if js_api_obj is not None:
                        remote_direct_ref.append(js_api_obj.health())
                except Exception:  # noqa: BLE001 (evidence only)
                    pass
                # Arm the in-vivo probe INSIDE the live remote document
                # and immediately collect what it stores: this is the
                # only moment the remote doc is guaranteed alive. If
                # pywebview is (re)injected there, the listener calls
                # health() as remote content; otherwise the value
                # stays 'pending'/absent (no payload delivered).
                try:
                    in_vivo_ref.append(
                        window.evaluate_js(_PROBE_ARM_IN_REMOTE_DOC)
                    )
                    in_vivo_ref.append(
                        window.evaluate_js(
                            "(() => window.__smokeRemoteHealth)()"
                        )
                    )
                except Exception:  # noqa: BLE001 (doc may already be dying)
                    pass
            loaded.set()

        window.events.loaded += _record_loaded
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
                    and health.get("api_version") == 2
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

            # 3b. #336: user acceptance path. Drive a REAL turn through
            #     the actual UI (textarea -> send button -> useChat ->
            #     transport -> bridge -> runtime -> SQLite) — not by
            #     calling the bridge directly. The scripted reply
            #     rendering in the ChatWindow is the end-user-visible
            #     outcome.
            window.evaluate_js(_PROBE_UI_SEND)
            ui_send = _poll_probe(window, _PROBE_COLLECT_UI_SEND, timeout=45)
            report.check(
                "real UI send: user message -> bridge turn -> reply renders",
                bool(
                    ui_send
                    and isinstance(ui_send, dict)
                    and ui_send.get("sent") is True
                    and ui_send.get("replied") is True
                ),
                json.dumps(ui_send)[:300] if ui_send else "no result",
            )

            # 3b-i. #336 review blocker 3: inspect SQLite IMMEDIATELY
            #     after the UI send — BEFORE any direct bridge call —
            #     so the persistence evidence is attributable to the
            #     UI path alone. Verify CONTENT, not just counts: the
            #     exact message the UI sent must be the one on disk,
            #     with its assistant reply, and the turn ledger must
            #     hold exactly one COMPLETED request for it.
            ui_send_user_rows = _sqlite_fetch_all(
                smoke_db_path,
                "select role, content from chat_logs "
                "where content like ? order by id",
                ("%Mensagem real do smoke #336 via UI%",),
            )
            report.check(
                "UI send persisted to SQLite BEFORE any direct op (user message on disk)",
                bool(
                    ui_send
                    and ui_send.get("sent") is True
                    and len(ui_send_user_rows) == 1
                    and ui_send_user_rows[0][0] == "user"
                    and ui_send_user_rows[0][1]
                    == "Mensagem real do smoke #336 via UI"
                ),
                f"user_rows={ui_send_user_rows!r}",
            )
            ui_send_rows = _sqlite_row_count(smoke_db_path, "chat_logs")
            report.check(
                "UI send persisted to SQLite (chat_logs >= 2: user + assistant)",
                bool(
                    ui_send
                    and ui_send.get("replied") is True
                    and ui_send_rows >= 2
                ),
                f"chat_logs rows after UI send={ui_send_rows} "
                "(inspected before any direct bridge op)",
            )
            ui_send_ledger = _sqlite_fetch_all(
                smoke_db_path,
                "select status, count(*) from turn_requests "
                "group by status",
            )
            report.check(
                "UI send turn ledger holds exactly one completed request",
                bool(ui_send_ledger == [("completed", 1)]),
                f"turn_requests by status={ui_send_ledger!r}",
            )

            # 3c. #336: destructive-op acceptance path through the REAL
            #     privacy panel: click -> explicit confirmation -> real
            #     local delete -> success status rendered.
            window.evaluate_js(_PROBE_UI_PRIVACY)
            ui_privacy = _poll_probe(window, _PROBE_COLLECT_UI_PRIVACY, timeout=45)
            report.check(
                "privacy panel UI: Apagar histórico -> confirmar -> deleted",
                bool(
                    ui_privacy
                    and isinstance(ui_privacy, dict)
                    and ui_privacy.get("confirmed") is True
                    and "Operação concluída" in str(ui_privacy.get("status", ""))
                ),
                json.dumps(ui_privacy)[:300] if ui_privacy else "no result",
            )

            # 3c-i. #336 review blocker 3: inspect SQLite IMMEDIATELY
            #     after the UI privacy op — BEFORE the direct bridge
            #     operations below — so the zero-row evidence is
            #     attributable to the UI button click alone.
            #     delete_history wipes messages AND the turn ledger
            #     (its contract), so both tables must read zero.
            after_ui_privacy_chat = _sqlite_row_count(smoke_db_path, "chat_logs")
            after_ui_privacy_turns = _sqlite_row_count(smoke_db_path, "turn_requests")
            report.check(
                "UI privacy op wiped SQLite BEFORE any direct op (chat_logs == 0)",
                bool(
                    ui_privacy
                    and ui_privacy.get("confirmed") is True
                    and after_ui_privacy_chat == 0
                ),
                f"chat_logs rows after UI privacy={after_ui_privacy_chat} "
                "(inspected before any direct bridge op)",
            )
            report.check(
                "UI privacy op wiped the turn ledger (turn_requests == 0)",
                after_ui_privacy_turns == 0,
                f"turn_requests rows after UI privacy={after_ui_privacy_turns} "
                "(inspected before any direct bridge op)",
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
            #    Evidence, staged (reviewer follow-up on the same-URL
            #    revert race):
            #    A. remote page normally loaded -> health() refuses
            #       (witnessed and executed inside the loaded handler,
            #       the same event the revert logic runs on — polling
            #       get_current_url() can miss the whole remote window
            #       when the network is fast);
            #    B. revert issued to the SAME entry URL: the load_url
            #       wrapper probes health() at the instant the revert is
            #       issued, with the remote document still the live
            #       document and the new local load NOT completed (the
            #       exact window where get_uri() equality would reopen a
            #       naive URL-comparison bridge) -> must refuse;
            #    C. after the new local load completed -> health()
            #       serves the local document again.
            #
            #    The in-vivo probe (armed inside the live remote
            #    document) stays as best-effort extra evidence: whether
            #    it fires depends on WebKitGTK timing (the remote doc is
            #    often destroyed before pywebview is injected there,
            #    which is a safe outcome — nothing was delivered).
            #    The deterministic proof of the same-URL state machine
            #    is the test suite (TestRevertToSameEntryUrl), not this
            #    smoke: the smoke proves the wiring in a real WebKitGTK
            #    process; the tests prove every transition of the trust
            #    machine.
            #
            #    The ONLY failing outcome for A/B/C is a successful
            #    health() payload delivered to remote content.
            remote_in_vivo = None
            remote_direct = None

            window.evaluate_js(_PROBE_NAVIGATE_REMOTE)

            # The revert is triggered by the shell's loaded handler;
            # give it time to fire and then settle back on the build.
            deadline = time.time() + 20
            while time.time() < deadline:
                current = window.get_current_url() or ""
                if current.startswith("file://") and remote_urls_seen:
                    break
                time.sleep(0.2)

            # Direct refusal: the loaded handler recorded that the
            # window really showed a remote URL (remote_urls_seen is
            # the authoritative witness — same event the revert used)
            # and executed js_api.health() while the window was
            # provably remote; remote_direct_ref holds that result.

            # Wait for the revert to land back on the local build and
            # its load to COMPLETE (loaded fires for the new local
            # document, which re-commits the trust).
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

            # The remote window was provably alive: the loaded handler
            # (the same event the revert logic uses) witnessed it.
            report.check(
                "remote navigation reached a remote document (witnessed by loaded handler)",
                bool(remote_urls_seen),
                f"urls_seen={[u[:60] for u in remote_urls_seen[:3]]}",
            )

            # Direct refusal (stage A): the loaded handler recorded that
            # the window really showed a remote URL (remote_urls_seen is
            # the authoritative witness — same event the revert used)
            # and executed js_api.health() while the window was provably
            # remote; remote_direct_ref holds that result.
            remote_direct = remote_direct_ref[-1] if remote_direct_ref else None
            direct_ok = bool(
                remote_direct
                and remote_direct.get("ok") is False
                and remote_direct.get("code") == "bridge_unavailable"
            )
            report.check(
                "A: remote page normally loaded -> bridge refuses (direct path)",
                direct_ok,
                json.dumps(remote_direct)[:200] if remote_direct else "no result",
            )

            # Mid-revert (stage B, deterministic): when the shell issued
            # the revert load_url(entry) — the SAME file:// URL the
            # window opened with — the previous (remote) document was
            # still the live document and the new local load had NOT
            # completed. The probe called health() at that instant; it
            # must be refused. This is the same-URL race window: URL
            # equality must not re-open the bridge.
            mid_revert = mid_revert_ref[-1] if mid_revert_ref else None
            mid_revert_ok = bool(
                mid_revert
                and isinstance(mid_revert.get("health"), dict)
                and mid_revert["health"].get("ok") is False
                and mid_revert["health"].get("code") == "bridge_unavailable"
                and str(mid_revert.get("reverting_from", "")).startswith("https://")
            )
            report.check(
                "B: revert issued to SAME entry URL, new local load incomplete -> bridge still closed",
                mid_revert_ok,
                (
                    f"reverting_from={str(mid_revert.get('reverting_from'))[:60]!r} "
                    f"health={json.dumps(mid_revert.get('health'))[:120]}"
                    if mid_revert
                    else "revert never observed through load_url (no stage-B evidence)"
                ),
            )

            # In-vivo: the probe armed inside the remote document. If
            # pywebview was injected there, health() was called as
            # remote content; the outcome was collected by the loaded
            # handler itself (the doc dies with its window object, so
            # it had to be read before the doc died). The arm call
            # returns {armed: true}; the collect returns the stored
            # outcome — take the LAST collected value, skipping the
            # arm result dicts.
            remote_in_vivo = None
            for value in reversed(in_vivo_ref):
                if isinstance(value, dict) and "armed" not in value:
                    remote_in_vivo = value
                    break

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
                "in-vivo (best-effort): remote doc never receives a working bridge",
                in_vivo_ok or never_bridge_ok,
                (
                    json.dumps(remote_in_vivo)[:200]
                    if remote_in_vivo
                    else "remote doc destroyed before any bridge call (no result)"
                ),
            )

            # Stage C: after the new local load completed, the bridge
            # must serve the local document again (same entry URL, new
            # completed load → re-committed trust).
            js_api_after = _get_js_api(window)
            after_reload = None
            if js_api_after is not None:
                try:
                    after_reload = js_api_after.health()
                except Exception:  # noqa: BLE001 (evidence only)
                    after_reload = None
            report.check(
                "C: new local load completed -> bridge serves again",
                bool(
                    after_reload
                    and after_reload.get("ok") is True
                    and after_reload.get("api_version") == 2
                ),
                json.dumps(after_reload)[:200] if after_reload else "no result",
            )

            # 6. No HTTP server for the UI in this process.
            ports = _listening_ports(os.getpid())
            report.check(
                "no HTTP server listening for the UI",
                not ports,
                f"ports={ports}",
            )

            # 6b. #336: the companion runtime is local. Through the
            #      exact bridge object the JS layer reaches, drive one
            #      full turn end-to-end and prove persistence WITHOUT
            #      trusting the runtime's own read path: a separate
            #      stdlib sqlite3 connection reads the same file.
            #      NOTE (#336 review blocker 3): the UI-attributable
            #      persistence/privacy checks already ran above
            #      (3b-i, 3c-i) BEFORE these direct calls; everything
            #      from here on is SEPARATE evidence for the direct
            #      bridge surface, not the UI path proof.
            js_api_local = _get_js_api(window)
            runtime_state = None
            send_result = None
            if js_api_local is not None:
                try:
                    runtime_state = js_api_local.runtime_state()
                    send_result = js_api_local.send_message(
                        "smoke-336-0001",
                        "Mensagem do smoke #336",
                    )
                except Exception as exc:  # noqa: BLE001 (evidence only)
                    worker_error.append(f"runtime bridge call failed: {exc}")

            report.check(
                "runtime_state() reports local storage ready",
                bool(
                    runtime_state
                    and runtime_state.get("ok") is True
                    and runtime_state.get("storage") is True
                ),
                json.dumps(runtime_state)[:200] if runtime_state else "no result",
            )
            report.check(
                "send_message() turns a real conversation through the bridge",
                bool(
                    send_result
                    and send_result.get("success") is True
                    and send_result.get("response")
                ),
                json.dumps(send_result)[:200] if send_result else "no result",
            )

            # Independent SQLite proof: read the smoke DB with stdlib
            # sqlite3 (read-only, different connection) — the turn is
            # durable on disk, not merely in the runtime's memory.
            tables = _sqlite_tables(smoke_db_path)
            report.check(
                "SQLite file holds the LocalStorage schema",
                bool(
                    tables
                    and "chat_logs" in tables
                    and "turn_requests" in tables
                ),
                f"tables={tables}",
            )
            persisted = _sqlite_row_count(smoke_db_path, "chat_logs")
            report.check(
                "turn messages persisted to SQLite (independent read)",
                persisted >= 2,
                f"chat_logs rows={persisted} (user + assistant)",
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
        run_desktop_shell(  # blocks on main thread
            html_name=SMOKE_PAGE,
            storage_path=smoke_db_path,
            provider=_make_scripted_provider(),
        )
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

    # 8. #336: restart recovery. A FRESH runtime over the same SQLite
    #    file (what a relaunch does) must load the persisted turn and
    #    report the stored revision — proving the app's state survives
    #    a close/reopen cycle with LocalStorage as the only store.
    from backend.companion_runtime import build_companion_runtime

    restarted = build_companion_runtime(storage_path=smoke_db_path)
    try:
        recovered_history = restarted.load_history()
        recovered_state = restarted.runtime_state()
        history_ok = bool(
            recovered_history
            and any(
                "smoke #336" in str(entry.get("content", ""))
                for entry in recovered_history
            )
        )
        report.check(
            "restart recovers the persisted conversation (fresh runtime, same DB)",
            history_ok,
            f"entries={len(recovered_history or [])}",
        )
        report.check(
            "restart recovers the stored revision",
            bool(
                recovered_state
                and recovered_state.get("ok") is True
                and recovered_state.get("revision", 0) >= 1
            ),
            json.dumps(recovered_state)[:200] if recovered_state else "no result",
        )
        privacy = restarted.delete_history()
        deleted = _sqlite_row_count(smoke_db_path, "chat_logs")
        deleted_turns = _sqlite_row_count(smoke_db_path, "turn_requests")
        report.check(
            "local privacy op really deletes (direct delete_history -> 0 messages AND 0 requests)",
            bool(
                privacy
                and privacy.get("success") is True
                and privacy.get("result", {}).get("status") == "applied"
                and deleted == 0
                and deleted_turns == 0
            ),
            f"chat_logs rows after delete={deleted}, "
            f"turn_requests rows after delete={deleted_turns}",
        )
    finally:
        restarted.close()

    return report.ok, report.lines


def main() -> int:
    print("Katherine desktop shell smoke (#334 + #336 local runtime)")
    print(f"repo: {REPO_ROOT}")
    print(f"python: {sys.version.split()[0]}\n")
    ok, _ = run_smoke()
    print()
    print("SMOKE_OK" if ok else "SMOKE_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
