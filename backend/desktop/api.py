"""Minimal, allowlisted Desktop bridge exposed to the pywebview frontend (#334).

Design rules enforced here and covered by ``test_desktop_api.py``:

* The object actually handed to pywebview's ``js_api`` is
  :class:`DesktopBridge`, a *facade* over :class:`DesktopApi`. The facade
  guarantees that **no exposed method ever raises**: every call returns
  plain JSON-serializable data. pywebview (6.2.1) wraps uncaught
  exceptions from ``js_api`` methods into a JavaScript ``Error`` carrying
  ``message``/``name``/``stack``; the facade makes that path unreachable
  by construction, returning sanitized structured payloads instead.
* The exposed surface is an explicit allowlist (``DESKTOP_API_METHODS``).
  This proof deliberately exposes exactly one method: ``health()``.
  pywebview walks ``dir(obj)`` and exposes *every public attribute*, so
  the facade keeps only allowlisted, bound wrappers public. Anything
  else lives on ``DesktopApi`` (never passed to ``js_api``) or in module
  functions.
* Every method validates its input. Invalid input returns a *sanitized*,
  structured payload: no tracebacks, no types, no paths, no environment
  details leak to the UI.
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

#: Messages are deliberately generic and free of internal detail.
_MSG_UNKNOWN_METHOD = "Unknown method."
_MSG_INTERNAL = "The desktop bridge failed to complete the request."
_MSG_UNEXPECTED_RESPONSE = "Unexpected bridge response."


class DesktopApiError(Exception):
    """Sanitized, structured error condition inside the bridge.

    Never crosses the JS boundary as an exception: the facade converts it
    into its ``payload`` dict. The payload carries a stable ``code`` plus
    a human-readable ``message`` that contains no internal detail.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = {"ok": False, "code": code, "message": message}


class DesktopApi:
    """Implementation of the allowlisted methods (no ``js_api`` sanitization).

    This class holds the actual behavior. It is deliberately *not* passed
    to pywebview: methods may raise :class:`DesktopApiError` for invalid
    input; the :class:`DesktopBridge` facade turns that into data.
    Keeping implementation apart from the exposed facade also keeps the
    ``js_api`` surface provably equal to ``DESKTOP_API_METHODS``.
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


class DesktopBridge:
    """The object actually delivered to pywebview's ``js_api`` (#334).

    Hard boundary guarantees:

    * Only the allowlisted wrappers created in ``__init__`` are public
      (and therefore exposed by pywebview's ``get_functions`` walk).
    * Each wrapper can never raise: invalid input, unexpected internal
      failures, and non-dict results all collapse into a sanitized,
      structured error payload. pywebview's exception-to-JS-``Error``
      path (which would carry ``stack``) is unreachable for this object.
    * There is no generic dispatch: no ``__getattr__``, no
      ``__call__``, no attribute passthrough. Calling an allowlisted
      method with bad input returns data, never an exception.
    """

    def __init__(self, api: DesktopApi | None = None) -> None:
        # Bind private wrappers; deliberately not using types.MethodType on
        # module-level functions to keep them out of the public surface.
        self._api = api if api is not None else DesktopApi()
        self._handlers = {
            "health": self._api.health,
        }
        # Sanity: the allowlist and the bound surface must match exactly,
        # at construction time (fail fast in dev/test, never in JS).
        if tuple(self._handlers) != DESKTOP_API_METHODS:
            raise RuntimeError("desktop bridge surface out of sync")  # pragma: no cover

    # -- allowlisted public surface (exposed to JS) ----------------------

    def health(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.health``; never raises."""
        return self._invoke("health", args)

    # -- boundary internals (never exposed: underscore-prefixed) ---------

    def _invoke(self, method: str, args: tuple[Any, ...]) -> dict[str, Any]:
        handler = self._handlers.get(method)
        if handler is None:
            return {"ok": False, "code": _ERROR_INVALID_INPUT, "message": _MSG_UNKNOWN_METHOD}
        try:
            result = handler(*args)
        except DesktopApiError as err:
            return err.payload
        except Exception:  # noqa: BLE001 (boundary: never leak internals to JS)
            return {"ok": False, "code": _ERROR_INTERNAL, "message": _MSG_INTERNAL}
        if not isinstance(result, dict):
            return {"ok": False, "code": _ERROR_INTERNAL, "message": _MSG_UNEXPECTED_RESPONSE}
        return result


def make_js_api() -> DesktopBridge:
    """Build the sanitized bridge object to pass as ``js_api=`` (#334).

    This is the single construction path used by the shell entrypoint;
    tests assert that ``run_desktop_shell`` hands exactly this kind of
    object to pywebview, so the sanitized facade is the *real* boundary
    (not an auxiliary helper disconnected from the exposed path).
    """
    return DesktopBridge(DesktopApi())
