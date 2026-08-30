"""Minimal, allowlisted Desktop API exposed to the pywebview frontend (#334).

Design rules enforced here and covered by ``test_desktop_api.py``:

* The exposed surface is an explicit allowlist (``DESKTOP_API_METHODS``).
  This proof deliberately exposes exactly one method: ``health()``.
* Every method returns plain JSON-serializable data. Internal objects,
  modules, settings, clients, or engines are never handed to JS.
* Every method validates its input. Invalid input raises
  :class:`DesktopApiError` carrying a *sanitized*, structured payload:
  no tracebacks, no types, no paths, no environment details leak to the UI.
* The module is import-pure: no side effects, no environment reads, no
  pywebview import. Window concerns live in :mod:`backend.desktop.app`.
"""

from __future__ import annotations

from typing import Any

#: The one and only public surface of the desktop bridge for this proof.
#: Adding a method here is a deliberate, reviewable decision.
DESKTOP_API_METHODS: tuple[str, ...] = ("health",)

#: Version of the desktop bridge contract. The frontend can feature-check
#: against this single integer instead of sniffing for methods.
DESKTOP_API_VERSION = 1

#: Public error codes. Structured, stable, safe to show in the UI.
_ERROR_INVALID_INPUT = "invalid_input"
_ERROR_INTERNAL = "internal_error"


class DesktopApiError(Exception):
    """Sanitized, structured error surfaced to the JS side.

    The payload is the only thing the UI ever sees: ``code`` plus a
    human-readable ``message`` that contains no internal detail.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = {"ok": False, "code": code, "message": message}


class DesktopApi:
    """Explicit, allowlisted API object handed to pywebview's ``js_api``.

    pywebview exposes **every public attribute** of this object to
    JavaScript, so the class must stay minimal by construction: anything
    public becomes bridge surface. The tests assert the public surface
    equals ``DESKTOP_API_METHODS`` exactly.
    """

    def health(self, *args: Any) -> dict[str, Any]:
        """Round-trip proof of the JS ↔ Python bridge.

        Returns a stable, structured payload so the frontend can verify
        the bridge works without depending on any domain behavior.

        Takes no arguments: stray JS-side arguments are rejected with a
        sanitized ``invalid_input`` error instead of being ignored.
        """
        if args:
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, "health() takes no arguments."
            )
        return {"ok": True, "api_version": DESKTOP_API_VERSION}


def safe_call(api: DesktopApi, method_name: str, *args: Any) -> dict[str, Any]:
    """Guard used by the shell layer to sanitize unexpected failures.

    Valid, allowlisted calls are forwarded. Unknown methods, invalid
    arguments, and unexpected internal failures all become a sanitized,
    structured error payload — never a traceback in the UI.

    This stays in the module (not on the API object) precisely so it is
    not exposed to JavaScript: pywebview only exposes public members of
    the ``js_api`` instance.
    """
    if method_name not in DESKTOP_API_METHODS or not hasattr(api, method_name):
        return {"ok": False, "code": _ERROR_INVALID_INPUT, "message": "Unknown method."}
    try:
        result = getattr(api, method_name)(*args)
    except DesktopApiError as err:
        return err.payload
    except Exception:  # noqa: BLE001 (boundary: never leak internals to JS)
        return {
            "ok": False,
            "code": _ERROR_INTERNAL,
            "message": "The desktop bridge failed to complete the request.",
        }
    if not isinstance(result, dict):
        return {
            "ok": False,
            "code": _ERROR_INTERNAL,
            "message": "Unexpected bridge response.",
        }
    return result
