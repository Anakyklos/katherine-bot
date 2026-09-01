"""Allowlisted Desktop bridge exposed to the pywebview frontend (#334, #336).

Design rules enforced here and covered by ``test_desktop_api.py`` /
``test_desktop_navigation.py``:

* The object actually handed to pywebview's ``js_api`` is
  :class:`DesktopBridge`, a *facade* over :class:`DesktopApi`. The facade
  guarantees that **no exposed method ever raises**: every call returns
  plain JSON-serializable data. pywebview (6.2.1) wraps uncaught
  exceptions from ``js_api`` methods into a JavaScript ``Error`` carrying
  ``message``/``name``/``stack``; the facade makes that path unreachable
  by construction, returning sanitized structured payloads instead.
* The exposed surface is an explicit allowlist (``DESKTOP_API_METHODS``).
  pywebview walks ``dir(obj)`` and exposes *every public attribute*, so
  the facade keeps only allowlisted, bound wrappers public. Anything
  else lives on ``DesktopApi`` (never passed to ``js_api``) or in module
  functions.
* Every method validates its input. Invalid input returns a *sanitized*,
  structured payload: no tracebacks, no types, no paths, no environment
  details leak to the UI — and no input content is ever echoed back.
* The runtime object injected into :class:`DesktopApi` is the local
  :class:`~backend.companion_runtime.CompanionRuntime` (#336). The
  bridge never imports it: it calls the small, validated method set and
  passes through the runtime's already-sanitized payload shapes.
* The module is import-pure: no side effects, no environment reads, no
  pywebview import, no companion_runtime import. Window concerns live in
  :mod:`backend.desktop.app`.
"""

from __future__ import annotations

from typing import Any

#: The complete public surface of the desktop bridge (#334: ``health``;
#: #336: the companion conversation + privacy surface). Adding a method
#: here is a deliberate, reviewable decision.
DESKTOP_API_METHODS: tuple[str, ...] = (
    "health",
    "runtime_state",
    "load_history",
    "send_message",
    "delete_history",
    "delete_memories",
    "reset_emotional_state",
    "reset_relationship_state",
)

#: Version of the desktop bridge contract. The frontend can feature-check
#: against this single integer instead of sniffing for methods.
DESKTOP_API_VERSION = 2

#: Public error codes. Structured, stable, safe to show in the UI.
_ERROR_INVALID_INPUT = "invalid_input"
_ERROR_INTERNAL = "internal_error"

#: Messages are deliberately generic and free of internal detail (and
#: never contain the offending input).
_MSG_UNKNOWN_METHOD = "Unknown method."
_MSG_INTERNAL = "The desktop bridge failed to complete the request."
_MSG_UNEXPECTED_RESPONSE = "Unexpected bridge response."
_MSG_NO_RUNTIME = "The desktop runtime is not available."

#: History window bounds (validated here before reaching the runtime).
_HISTORY_LIMIT_MIN = 1
_HISTORY_LIMIT_MAX = 500
_HISTORY_LIMIT_DEFAULT = 50

#: Request-id bounds, mirroring the LocalStorage contract exactly.
_REQUEST_ID_MAX = 128
_REQUEST_ID_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)

#: Message bounds, mirroring the web contract.
_MESSAGE_MAX = 10_000

#: Message body must not contain C0 control characters except tab/newline/
#: carriage return (defensive: JSON transport handles them, but control
#: garbage is rejected before it ever reaches the runtime).
_MESSAGE_FORBIDDEN_CONTROL = frozenset(
    chr(c) for c in range(0x20) if chr(c) not in "\t\n\r"
)


class DesktopApiError(Exception):
    """Sanitized, structured error condition inside the bridge.

    Never crosses the JS boundary as an exception: the facade converts it
    into its ``payload`` dict. The payload carries a stable ``code`` plus
    a human-readable ``message`` that contains no internal detail.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = {"ok": False, "code": code, "message": message}


# ---------------------------------------------------------------------------
# Input validation (pure, total, never raises)
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_history_limit(limit: Any) -> bool:
    """True iff ``limit`` is an int in [1, 500] (bool excluded)."""
    if not _is_int(limit):
        return False
    return _HISTORY_LIMIT_MIN <= limit <= _HISTORY_LIMIT_MAX


def _valid_request_id(request_id: Any) -> bool:
    """True iff ``request_id`` is a non-empty str, allowed charset, bounded."""
    if not isinstance(request_id, str) or not request_id:
        return False
    if len(request_id) > _REQUEST_ID_MAX:
        return False
    return all(ch in _REQUEST_ID_ALLOWED for ch in request_id)


def _valid_message(message: Any) -> bool:
    """True iff ``message`` is a non-empty str within bounds, no control garbage."""
    if not isinstance(message, str):
        return False
    if not message.strip():
        return False
    if len(message) > _MESSAGE_MAX:
        return False
    return not any(ch in _MESSAGE_FORBIDDEN_CONTROL for ch in message)


# ---------------------------------------------------------------------------
# Implementation (never passed to pywebview)
# ---------------------------------------------------------------------------


class DesktopApi:
    """Implementation of the allowlisted methods (no ``js_api`` sanitization).

    This class holds the actual behavior. It is deliberately *not* passed
    to pywebview: methods may raise :class:`DesktopApiError` for invalid
    input; the :class:`DesktopBridge` facade turns that into data.
    Keeping implementation apart from the exposed facade also keeps the
    ``js_api`` surface provably equal to ``DESKTOP_API_METHODS``.

    ``runtime`` is the local companion runtime (#336,
    ``backend.companion_runtime.CompanionRuntime``). It is injected as a
    plain object with the small method set the bridge calls — the bridge
    never imports the runtime module (import purity) and never touches
    anything beyond these methods. ``None`` fails every companion call
    closed with a sanitized ``internal_error`` payload.
    """

    def __init__(self, runtime: Any = None) -> None:
        self._runtime = runtime

    # -- #334: round-trip probe -------------------------------------------

    def health(self, *args: Any) -> dict[str, Any]:
        """Round-trip proof of the JS ↔ Python bridge.

        Returns a stable, structured payload so the frontend can verify
        the bridge works without depending on any domain behavior.

        Takes no arguments: stray JS-side arguments are rejected with a
        sanitized ``invalid_input`` error instead of being ignored.
        """
        if args:
            raise DesktopApiError(_ERROR_INVALID_INPUT, "health() takes no arguments.")
        return {"ok": True, "api_version": DESKTOP_API_VERSION}

    # -- #336: readiness + configuration ----------------------------------

    def runtime_state(self, *args: Any) -> dict[str, Any]:
        """Readiness probe: storage open + provider-configured flag.

        Takes no arguments. The payload shape is the runtime's
        ``runtime_state()`` dict (ok/storage/provider_configured/revision)
        wrapped in ``ok: True`` on success.
        """
        if args:
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, "runtime_state() takes no arguments."
            )
        runtime = self._require_runtime()
        result = runtime.runtime_state()
        return dict(result)

    # -- #336: conversation -----------------------------------------------

    def load_history(self, *args: Any) -> dict[str, Any]:
        """Bounded history window: ``load_history(limit?)`` with limit in [1, 500]."""
        limit = _HISTORY_LIMIT_DEFAULT
        if len(args) > 1:
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, "load_history() takes at most one argument."
            )
        if args:
            if not _valid_history_limit(args[0]):
                raise DesktopApiError(
                    _ERROR_INVALID_INPUT, "load_history() received an invalid limit."
                )
            limit = args[0]
        runtime = self._require_runtime()
        messages = runtime.load_history(limit=limit)
        if not isinstance(messages, list):
            raise DesktopApiError(_MSG_INTERNAL.__class__.__name__, _MSG_INTERNAL)
        return {"ok": True, "messages": messages}

    def send_message(self, *args: Any) -> dict[str, Any]:
        """One conversation turn: ``send_message(request_id, message)``.

        Both arguments are validated here (charset, bounds, no control
        garbage) before the runtime is ever called. The runtime returns a
        sanitized turn payload (success/error_code/error_message); it is
        passed through with only ``ok`` added.
        """
        if len(args) != 2:
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, "send_message() takes exactly two arguments."
            )
        request_id, message = args
        if not _valid_request_id(request_id):
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, "send_message() received an invalid request id."
            )
        if not _valid_message(message):
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, "send_message() received an invalid message."
            )
        runtime = self._require_runtime()
        result = runtime.send_turn(request_id=request_id, message=message)
        if not isinstance(result, dict):
            raise DesktopApiError(_ERROR_INTERNAL, _MSG_UNEXPECTED_RESPONSE)
        payload = dict(result)
        # Transport-level flag: every send payload answers "did this
        # turn succeed?" through the same key the other ops use.
        payload["ok"] = payload.get("success") is True
        return payload

    # -- #336: privacy operations ------------------------------------------

    def delete_history(self, *args: Any) -> dict[str, Any]:
        """Erase the local conversation history (transactional)."""
        return self._privacy_op("delete_history", args)

    def delete_memories(self, *args: Any) -> dict[str, Any]:
        """Erase the locally stored memories (transactional)."""
        return self._privacy_op("delete_memories", args)

    def reset_emotional_state(self, *args: Any) -> dict[str, Any]:
        """Reset the emotional state to neutral (transactional)."""
        return self._privacy_op("reset_emotional_state", args)

    def reset_relationship_state(self, *args: Any) -> dict[str, Any]:
        """Reset the relationship state to neutral (transactional)."""
        return self._privacy_op("reset_relationship_state", args)

    # -- internals ----------------------------------------------------------

    def _privacy_op(self, name: str, args: tuple[Any, ...]) -> dict[str, Any]:
        if args:
            raise DesktopApiError(
                _ERROR_INVALID_INPUT, f"{name}() takes no arguments."
            )
        runtime = self._require_runtime()
        result = getattr(runtime, name)()
        if not isinstance(result, dict):
            raise DesktopApiError(_ERROR_INTERNAL, _MSG_UNEXPECTED_RESPONSE)
        payload = dict(result)
        if payload.get("success") is True:
            payload["ok"] = True
        else:
            payload["ok"] = False
        return payload

    def _require_runtime(self) -> Any:
        runtime = self._runtime
        if runtime is None:
            # Programming error in wiring (never a JS-visible exception).
            raise DesktopApiError(_ERROR_INTERNAL, _MSG_NO_RUNTIME)
        return runtime


# ---------------------------------------------------------------------------
# The facade actually handed to pywebview
# ---------------------------------------------------------------------------


class DesktopBridge:
    """The object actually delivered to pywebview's ``js_api`` (#334, #336).

    Hard boundary guarantees:

    * Only the allowlisted wrappers created in ``__init__`` are public
      (and therefore exposed by pywebview's ``get_functions`` walk).
    * Each wrapper can never raise: invalid input, unexpected internal
      failures, and non-dict results all collapse into a sanitized,
      structured error payload. pywebview's exception-to-JS-``Error``
      path (which would carry ``stack``) is unreachable for this object.
    * There is no generic dispatch: no ``__getattr__``, no ``__call__``,
      no attribute passthrough. Calling an allowlisted method with bad
      input returns data, never an exception.
    """

    def __init__(self, api: DesktopApi | None = None, *, runtime: Any = None) -> None:
        # ``runtime`` is a convenience injection used by make_js_api; the
        # canonical construction path is DesktopApi(runtime=...).
        if api is None:
            api = DesktopApi(runtime=runtime)
        elif runtime is not None:
            raise ValueError("pass api or runtime, not both")
        self._api = api
        self._handlers: dict[str, Any] = {
            "health": self._api.health,
            "runtime_state": self._api.runtime_state,
            "load_history": self._api.load_history,
            "send_message": self._api.send_message,
            "delete_history": self._api.delete_history,
            "delete_memories": self._api.delete_memories,
            "reset_emotional_state": self._api.reset_emotional_state,
            "reset_relationship_state": self._api.reset_relationship_state,
        }
        # Sanity: the allowlist and the bound surface must match exactly,
        # at construction time (fail fast in dev/test, never in JS).
        if tuple(self._handlers) != DESKTOP_API_METHODS:
            raise RuntimeError("desktop bridge surface out of sync")  # pragma: no cover

    # -- allowlisted public surface (exposed to JS) ----------------------

    def health(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.health``; never raises."""
        return self._invoke("health", args)

    def runtime_state(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.runtime_state``; never raises."""
        return self._invoke("runtime_state", args)

    def load_history(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.load_history``; never raises."""
        return self._invoke("load_history", args)

    def send_message(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.send_message``; never raises."""
        return self._invoke("send_message", args)

    def delete_history(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.delete_history``; never raises."""
        return self._invoke("delete_history", args)

    def delete_memories(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper for ``DesktopApi.delete_memories``; never raises."""
        return self._invoke("delete_memories", args)

    def reset_emotional_state(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper; never raises."""
        return self._invoke("reset_emotional_state", args)

    def reset_relationship_state(self, *args: Any) -> dict[str, Any]:
        """Sanitized wrapper; never raises."""
        return self._invoke("reset_relationship_state", args)

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


def make_js_api(runtime: Any = None) -> DesktopBridge:
    """Build the sanitized bridge object to pass as ``js_api=`` (#334, #336).

    ``runtime`` is the local companion runtime. This is the single
    construction path used by the shell entrypoint; tests assert that
    ``run_desktop_shell`` hands exactly this kind of object to pywebview,
    so the sanitized facade is the *real* boundary (not an auxiliary
    helper disconnected from the exposed path).
    """
    return DesktopBridge(DesktopApi(runtime=runtime))
