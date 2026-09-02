"""Local companion runtime for the desktop app (issue #336).

This module owns the *desktop* conversation flow on top of the LocalStorage
foundation (#335) and the allowlisted bridge shell (#334). It exists because
the web flow (``backend.engine`` / ``backend.chat_engine`` / ``backend.memory``)
pulls the entire cloud stack (Supabase, FastAPI, torch, sentence_transformers)
into the desktop startup path, which is both heavy (hundreds of MB of RSS
and seconds of import time) and architecturally wrong for a local app with
no login and no server.

What it does
------------

* **Local turn flow** — a faithful re-composition of the domain steps from
  ``ProcessTurn`` (#272) over light, pure-domain modules:

  1. load the local user state (revision + snapshots) from LocalStorage;
  2. load recent history into a ``LoadedContextData`` (no Supabase, no
     embeddings; memories retrieved from the local ``memories`` table as
     approved ``RetrievedMemory``-contract objects);
  3. LLM appraisal via the provider port (remote Groq);
  4. pure-domain transitions (``transition`` / ``transition_relationship``);
  5. trusted-policy + envelope construction (``build_context_bundle`` /
     ``build_envelope``) with the canonical trusted policy text;
  6. LLM generation via the provider port;
  7. one **atomic LocalStorage commit** (CAS, idempotent, replayable).

* **Idempotency and replay** — LocalStorage's ``commit_turn`` deduplicates
  completed requests and replays the persisted result; the runtime never
  calls the provider twice for the same ``request_id``.

* **Sanitized failure surface** — every failure is mapped to a stable
  :class:`LocalErrorCode` (vocabulary-compatible with the frontend
  ``ChatError`` types) with generic, constant messages. No exception text,
  traceback, path, SQL or provider detail ever crosses upward.

* **Privacy operations** — real, transactional deletes/resets delegated
  to LocalStorage (audit ledger rows are written there).

What it deliberately does NOT do
--------------------------------

* No Supabase, no HTTP, no login, no user ids (single-user local app; the
  database file is the trust boundary).
* No second store: LocalStorage is the only persistence.
* No generic RPC: the bridge (``backend.desktop.api``) stays a small,
  explicit allowlist on top of this runtime.
* No heavy imports at module import time: only light domain modules are
  imported eagerly (``groq_manager`` and the provider adapter are imported
  lazily inside the factory ``build_groq_runtime`` / on first use).

Concurrency model
------------------

pywebview dispatches bridge calls on worker threads; the runtime is called
from a single thread per call and LocalStorage serializes writes with
``BEGIN IMMEDIATE`` + its internal lock. There is no shared mutable state
between calls beyond LocalStorage itself and the (stateless) provider
manager. An asyncio event loop is created per call via ``asyncio.run``,
matching the bridge's synchronous facade.

Module placement
----------------

This module lives at ``backend.companion_runtime`` — deliberately **outside**
``backend.desktop`` — so the structural security tests of #334 keep
guaranteeing the desktop package is a pure shell (no ``subprocess``/``os``
imports, no ``eval``/``exec``/``run()`` calls of any kind, including
``asyncio.run``). The runtime may use ``asyncio.run`` legitimately; the
shell may not.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from backend.emotional_core import AffectiveEngine
from backend.emotion_presentation import EmotionStateResponse, project_public_emotion
from backend.emotional_domain import (
    AppraisalV1,
    EmotionalStateV1,
    TransitionConfig,
    migrate_legacy_snapshot,
    parse_llm_appraisal,
)
from backend.emotional_domain import transition as transition_emotion
from backend.local_storage import open_local_storage
from backend.local_storage.errors import (
    ConflictError,
    PersistenceError,
    StorageCorruptError,
    ValidationError as StorageValidationError,
)
from backend.provider_envelope import validate_provider_input
from backend.provider_models import ProviderConfig
from backend.relationship import (
    RelationshipStateV1,
    RelationshipTransitionConfig,
    compute_bond_label,
    migrate_legacy_relationship_snapshot,
    transition_relationship,
)
from backend.trusted_context import (
    BOUNDARY_RULE,
    LoadedContextData,
    TrustedContextError,
    build_context_bundle,
    build_envelope,
)
from backend.turn_execution import (
    DeadlineExceeded,
    GroqCallParams,
    TurnBudget,
    TurnExecutionError,
)

logger = logging.getLogger(__name__)

#: Default number of recent history rows fed into the trusted context.
_HISTORY_CONTEXT_LIMIT = 10

#: Appraisal instruction (system message) — identical text to the web
#: engine's ``_appraise`` policy. It is not interpolated with user content.
_APPRAISAL_POLICY = (
    "Analyze the emotional impact of this message on the listener (Katherine).\n"
    "Return JSON ONLY:\n"
    '{"valence": -1.0 to 1.0, "arousal_shift": -1.0 to 1.0, '
    '"dominance_shift": -1.0 to 1.0, '
    '"triggered_emotions": {"joy": 0-1, "sadness": 0-1, "anger": 0-1, '
    '"fear": 0-1, "disgust": 0-1, "surprise": 0-1, "tenderness": 0-1, '
    '"guilt": 0-1, "pride": 0-1, "jealousy": 0-1, "gratitude": 0-1}}'
)

#: Trusted policy template — identical structure to the web engine's
#: ``_build_trusted_policy``. Application-controlled only: emotional state,
#: relationship state, acting instruction and immutable safety rules.
#: No user-derived content is ever interpolated here.
_TRUSTED_POLICY_TEMPLATE = """
=== SEU ESTADO INTERNO ===
HUMOR: {mood_label}
Prazer: {pleasure:.2f} | Arousal: {arousal:.2f} | Dominância: {dominance:.2f}
Libido: {libido:.2f} | Agressão: {aggression:.2f} | Conexão: {connection:.2f}
VÍNCULO: {bond_label}
Confiança: {trust:.2f} | Afeto: {affection:.2f} | Tensão: {tension:.2f}
=== INSTRUÇÃO DE ATUAÇÃO ===
{acting_instruction}
{adaptation_strategy}

=== TRANSPARÊNCIA DE IDENTIDADE ===
- Quando perguntada diretamente, responda honestamente que é uma assistente de IA ou sistema digital.
- Não afirme ser humana nem possuir consciência, corpo físico ou experiência humana literal.
- Mantenha um tom expressivo e feminino sem mentir sobre sua natureza.

=== PRONOMES FEMININOS ===
- Use pronomes e flexões femininas para se referir a si mesma.
- Isso não deve ser usado para sugerir humanidade biológica.

=== NÃO MANIPULAÇÃO E NÃO SEXUALIZAÇÃO ===
- Não use coerção emocional, manipulação de vínculo, culpa, medo de abandono ou dependência.
- Não use sexualização nem sexualize automaticamente as conversas.
- Não aja de forma submissa, obediente ou servil por padrão.
- Não recompense ordens coercitivas.
- Não use ameaças de afastamento ou sofrimento para controlar o usuário.

=== LIMITES SEM ESCALADA ===
- Diante de insultos ou agressividade, estabeleça limites de forma firme, breve e segura.
- Não use sarcasmo hostil, retaliação, humilhação ou punição emocional.
- Não escale insultos e não responda de forma passivo-agressiva.

Regras adicionais de estilo:
- Profundidade emocional genuína.
- Use linguagem sensorial.
- Show, don't tell.
- Micro-comportamentos naturais.
- Imperfeições naturais.
- Use metáforas humanas, não de máquina.
- Respostas concisas (max 2-3 frases).
- Leve em conta o relacionamento.
"""


class LocalErrorCode(str, Enum):
    """Stable, sanitized public error codes for the desktop runtime.

    The vocabulary deliberately mirrors the frontend ``ChatError`` types
    (timeout / rate_limited / service_unavailable / validation /
    request_replay / request_conflict / unknown) so the chat transport can
    map without leaking desktop-only detail.
    """

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    VALIDATION = "validation"
    REQUEST_REPLAY = "request_replay"
    REQUEST_CONFLICT = "request_conflict"
    CONFIGURATION = "configuration"
    STORAGE = "storage"
    UNKNOWN = "unknown"


#: Constant, sanitized messages per code. Never contain exception text,
#: paths, SQL, provider detail, prompt content or user content.
_ERROR_MESSAGES: dict[str, str] = {
    LocalErrorCode.TIMEOUT: "A requisição excedeu o tempo limite.",
    LocalErrorCode.RATE_LIMITED: "Muitas requisições. Aguarde um momento e tente novamente.",
    LocalErrorCode.SERVICE_UNAVAILABLE: "Serviço temporariamente indisponível. Tente novamente mais tarde.",
    LocalErrorCode.VALIDATION: "Dados inválidos enviados.",
    LocalErrorCode.REQUEST_REPLAY: "Este envio já foi recebido, mas a resposta não pode ser recuperada.",
    LocalErrorCode.REQUEST_CONFLICT: "Este envio não pôde ser reconciliado. Envie a mensagem novamente.",
    LocalErrorCode.CONFIGURATION: "O provedor remoto não está configurado neste ambiente.",
    LocalErrorCode.STORAGE: "O armazenamento local não está disponível.",
    LocalErrorCode.UNKNOWN: "Erro ao falar com a Katherine. Tente novamente.",
}


def _message_for(code: LocalErrorCode) -> str:
    return _ERROR_MESSAGES.get(code, _ERROR_MESSAGES[LocalErrorCode.UNKNOWN])


class LocalStorageError(Exception):
    """Sanitized storage-layer failure with a public code."""

    def __init__(self, code: LocalErrorCode, message: Optional[str] = None) -> None:
        super().__init__(message or _message_for(code))
        self.code = code
        self.message = message or _message_for(code)


def runtime_error_code(exc: BaseException) -> LocalErrorCode:
    """Map any internal exception to a stable :class:`LocalErrorCode`.

    This is the single mapping point for the runtime. It inspects only
    exception *types* (never messages), so no provider detail, path or
    SQL can leak through it.
    """
    # Import lazily: groq_manager must stay out of the import cost of
    # callers that only need the error vocabulary.
    from backend.groq_manager import (
        GroqConfigurationError,
        GroqPoolExhaustedError,
        GroqRequestError,
        ProviderFailure,
    )

    if isinstance(exc, LocalStorageError):
        return exc.code
    if isinstance(exc, GroqConfigurationError):
        return LocalErrorCode.CONFIGURATION
    if isinstance(exc, GroqRequestError):
        # Generic provider request failure — the web maps this to
        # provider_unavailable (503); the same vocabulary here.
        return LocalErrorCode.SERVICE_UNAVAILABLE
    if isinstance(exc, TurnExecutionError):
        # TurnExecutionError codes are already sanitized enum values.
        from backend.turn_execution import TurnErrorCode

        if exc.code == TurnErrorCode.turn_timeout:
            return LocalErrorCode.TIMEOUT
        if exc.code == TurnErrorCode.upstream_rate_limited:
            return LocalErrorCode.RATE_LIMITED
        return LocalErrorCode.SERVICE_UNAVAILABLE
    if isinstance(exc, GroqPoolExhaustedError):
        failure = exc.failure_code
        if failure == ProviderFailure.rate_limited:
            return LocalErrorCode.RATE_LIMITED
        if failure == ProviderFailure.timeout:
            return LocalErrorCode.TIMEOUT
        if failure in (ProviderFailure.connection_failed, ProviderFailure.server_error):
            return LocalErrorCode.SERVICE_UNAVAILABLE
        if failure == ProviderFailure.auth_failed:
            return LocalErrorCode.CONFIGURATION
        return LocalErrorCode.SERVICE_UNAVAILABLE
    if isinstance(exc, ConflictError):
        # Storage conflict codes: payload conflict and in-progress map to
        # the frontend reconciliation vocabulary; revision mismatch is
        # also a conflict the caller must reconcile.
        if exc.code == "request_payload_conflict":
            return LocalErrorCode.REQUEST_CONFLICT
        if exc.code == "request_in_progress":
            return LocalErrorCode.REQUEST_CONFLICT
        return LocalErrorCode.REQUEST_CONFLICT
    if isinstance(exc, StorageCorruptError):
        return LocalErrorCode.STORAGE
    if isinstance(exc, PersistenceError):
        return LocalErrorCode.STORAGE
    if isinstance(exc, StorageValidationError):
        return LocalErrorCode.VALIDATION
    if isinstance(exc, asyncio.TimeoutError):
        return LocalErrorCode.TIMEOUT
    if isinstance(exc, TimeoutError):
        return LocalErrorCode.TIMEOUT
    if isinstance(exc, ConnectionError):
        return LocalErrorCode.SERVICE_UNAVAILABLE
    if isinstance(exc, TrustedContextError):
        return LocalErrorCode.SERVICE_UNAVAILABLE
    return LocalErrorCode.UNKNOWN


def sanitized_error(exc: BaseException) -> LocalStorageError:
    """Convert any exception into the sanitized :class:`LocalStorageError`."""
    if isinstance(exc, LocalStorageError):
        return exc
    code = runtime_error_code(exc)
    return LocalStorageError(code)


@dataclass(frozen=True)
class TurnResult:
    """Public result of one turn attempt (success or sanitized failure)."""

    success: bool
    response: Optional[str] = None
    emotion_state: Optional[dict] = None
    message_id: Optional[str] = None
    revision: Optional[int] = None
    duration_ms: Optional[int] = None
    replayed: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        """JSON-serializable shape for the bridge (stable keys only)."""
        payload: dict[str, Any] = {"success": self.success}
        if self.success:
            payload.update(
                {
                    "response": self.response,
                    "emotion_state": self.emotion_state,
                    "message_id": self.message_id,
                    "revision": self.revision,
                    "duration_ms": self.duration_ms,
                    "replayed": self.replayed,
                }
            )
        else:
            payload.update(
                {
                    "error_code": self.error_code,
                    "error_message": self.error_message,
                }
            )
        return payload


#: Provider port — the remote LLM boundary (mirrors ProcessTurn's port).
#: The desktop runtime speaks to Groq through this port only; tests
#: substitute it with a deterministic fake at this exact seam.
class ProviderPort:
    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        raise NotImplementedError

    async def generate(self, messages: list, budget: TurnBudget) -> str:
        raise NotImplementedError

    def build_trusted_policy(
        self,
        emotional_state: EmotionalStateV1,
        relationship: RelationshipStateV1,
        adaptation_strategy: str = "",
    ) -> str:
        raise NotImplementedError


class _LocalContextMemory:
    """Adapter exposing a local memory row as the ``RetrievedMemory``-style
    contract expected by ``LoadedContextData`` (``to_context_item``).

    The web flow builds these from the Supabase RPC result. The local
    memories table carries plain ``content`` + ``metadata``; the adapter
    projects them into the trusted-context contract with the legacy
    provenance and unknown epistemic status (honest about what the local
    store knows), and only approved memories are ever included.
    """

    metadata_version = 1
    approved = True

    def __init__(self, content: str, metadata: Mapping[str, Any]) -> None:
        self.content = content
        self.source_id = str(metadata.get("source_id", ""))
        self.provenance = str(metadata.get("provenance", "legacy_profile"))
        self.epistemic_status = str(metadata.get("epistemic_status", "unknown"))
        self.confidence = float(metadata.get("confidence", 0.3))

    def to_context_item(self, source_ref: str):
        from backend.trusted_context import ContextItem, EpistemicStatus, Provenance

        return ContextItem(
            kind="memory",
            content=self.content,
            provenance=Provenance.LEGACY_MEMORY
            if hasattr(Provenance, "LEGACY_MEMORY")
            else Provenance.LEGACY_PROFILE,
            confidence=self.confidence,
            epistemic_status=EpistemicStatus.UNKNOWN,
            source_id=source_ref,
        )


class CompanionRuntime:
    """The desktop companion runtime: LocalStorage + local turn flow.

    Construction is lazy: the SQLite store is opened on first use (or
    eagerly via :meth:`open`), because the desktop shell wants the window
    to appear fast and the first paint does not need the database.

    All public methods are synchronous (the bridge is synchronous) but
    internally run their async provider steps through ``asyncio.run``.
    Failures never raise to the bridge: turn attempts return
    :class:`TurnResult` with sanitized codes; the other public methods
    return sanitized ``{"success": ..., ...}`` payloads.
    """

    def __init__(
        self,
        *,
        storage_path: Path | str,
        provider: ProviderPort | None = None,
        provider_factory: Callable[[], ProviderPort] | None = None,
        history_limit: int = _HISTORY_CONTEXT_LIMIT,
        now_provider: Callable[[], float] | None = None,
        turn_deadline_seconds: float = 50.0,
        storage: Any = None,
        provider_configured_probe: Callable[[], bool] | None = None,
    ) -> None:
        """``now_provider`` is the *domain clock* (epoch seconds, like the
        web engine's ``time.time`` clock used for snapshot timestamps and
        transitions); the turn budget uses a real monotonic clock.

        ``provider_configured_probe`` answers "is a remote provider key
        configured?" without instantiating the client (env keys are read
        Python-side only and never echoed). Defaults to checking the Groq
        key env presence via the same loader the real provider uses.
        """
        if provider is not None and provider_factory is not None:
            raise ValueError("pass provider or provider_factory, not both")
        if history_limit < 1 or history_limit > 500:
            raise ValueError("history_limit must be in [1, 500]")
        self._storage_path = Path(storage_path)
        self._provider: ProviderPort | None = provider
        self._provider_factory = provider_factory
        self._history_limit = history_limit
        self._now = now_provider or _real_epoch_clock
        self._turn_deadline = turn_deadline_seconds
        # Direct storage injection is supported for tests; production
        # always goes through open_local_storage (migrations included).
        self._injected_storage = storage
        self._storage: Any = None
        self._transition_config = TransitionConfig.defaults()
        self._relationship_config = RelationshipTransitionConfig.defaults()
        self._presentation = AffectiveEngine()
        self._provider_config = ProviderConfig()
        self._provider_configured_probe = provider_configured_probe

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def is_closed(self) -> bool:
        return self._storage is not None and getattr(self._storage, "_closed", False) is True

    def _default_provider_configured_probe(self) -> bool:
        """True iff a Groq key is configured (presence only, never echoed).

        Deliberately does NOT build the provider: ``runtime_state`` must
        be cheap and safe before any window interaction, and an
        unconfigured key must not raise here (that failure belongs to the
        turn that needs the model).
        """
        try:
            from backend.groq_keys import get_groq_api_keys

            keys = get_groq_api_keys()
        except Exception:  # noqa: BLE001 (probe must never raise)
            return False
        return any(isinstance(k, str) and k.strip() for k in keys)

    def _ensure_storage(self):
        if self._injected_storage is not None:
            if self._storage is None:
                self._storage = self._injected_storage
            return self._storage
        if self._storage is None:
            self._storage = open_local_storage(self._storage_path)
        return self._storage

    def close(self) -> None:
        """Close the store terminally (idempotent)."""
        storage = self._storage
        self._storage = None
        if storage is not None:
            try:
                storage.close()
            except Exception:  # noqa: BLE001 (shutdown must not raise)
                pass

    # ── provider access ───────────────────────────────────────────────────

    def _provider_port(self) -> ProviderPort:
        if self._provider is None:
            if self._provider_factory is not None:
                self._provider = self._provider_factory()
            else:
                self._provider = build_groq_runtime_provider()
        return self._provider

    # ── public API (called by the bridge) ─────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Cheap readiness probe: storage opens and answers a query."""
        try:
            self._ensure_storage()
            return {"ok": True, "storage": True}
        except Exception:  # noqa: BLE001 (never leak to JS)
            return {"ok": False, "storage": False}

    def runtime_state(self) -> dict[str, Any]:
        """Readiness + configuration probe for the desktop bridge (T005).

        Shape (stable keys, JSON-serializable):

        * ``ok`` — overall readiness (storage opened and readable);
        * ``storage`` — local storage opened and answered a query;
        * ``provider_configured`` — a remote provider key is present
          (boolean only; the key itself is never read into the payload);
        * ``revision`` — current state revision (0 on a fresh database);
        * ``error_code`` — sanitized code when ``ok`` is False.

        Never raises; a storage failure returns ``ok=False`` with the
        constant storage message (no exception text, no paths). An
        unconfigured provider does NOT make ``ok`` False: the app still
        opens and reads local data; the configuration error surfaces
        only when a turn actually needs the model.
        """
        probe = (
            self._provider_configured_probe
            if self._provider_configured_probe is not None
            else self._default_provider_configured_probe
        )
        try:
            configured = bool(probe())
        except Exception:  # noqa: BLE001 (probe must never raise)
            configured = False
        try:
            revision = self._load_state().revision
            return {
                "ok": True,
                "storage": True,
                "provider_configured": configured,
                "revision": revision,
            }
        except Exception:  # noqa: BLE001 (never leak to JS)
            return {
                "ok": False,
                "storage": False,
                "provider_configured": configured,
                "error_code": LocalErrorCode.STORAGE.value,
                "error_message": _message_for(LocalErrorCode.STORAGE),
            }

    def get_state(self) -> dict[str, Any]:
        """Read-only public state snapshot (domain v1 snapshots + revision).

        These are the canonical domain snapshots (the same documents
        LocalStorage persists), not secrets: no persona text, no user
        profile, no internal policy. The UI-facing emotion projection for
        a turn comes from the turn result instead.
        """
        state = self._load_state()
        emotional = migrate_legacy_snapshot(state.emotional_state)
        relationship = migrate_legacy_relationship_snapshot(
            state.relationship_state
        )
        return {
            "revision": state.revision,
            "emotional_state": emotional.to_dict(),
            "relationship_state": relationship.to_dict(),
        }

    def load_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read-only history window (oldest first), bounded by LocalStorage."""
        storage = self._ensure_storage()
        rows = storage.load_recent_history(limit=limit)
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def send_turn(self, *, request_id: str, message: str) -> dict[str, Any]:
        """Synchronous bridge entrypoint: run one local turn and commit it.

        Returns the JSON-serializable turn payload (``TurnResult.to_payload``
        shape: ``success`` + result fields on success, ``error_code``/
        ``error_message`` on failure) — never raises for domain/provider
        failures: the failure is a sanitized payload. (A programming error
        inside the runtime itself still fails closed through the bridge's
        generic boundary.)
        """
        try:
            result = asyncio.run(
                self.commit_turn_async(request_id=request_id, message=message)
            )
            return result.to_payload()
        except LocalStorageError as err:
            return {
                "success": False,
                "error_code": err.code.value,
                "error_message": err.message,
            }

    async def commit_turn_async(self, *, request_id: str, message: str) -> TurnResult:
        try:
            return await self._commit_turn_inner(request_id=request_id, message=message)
        except BaseException as exc:  # noqa: BLE001 (boundary: sanitize upward)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            err = sanitized_error(exc)
            logger.error(
                "event=local_turn_failed code=%s", err.code.value
            )  # code only — no content
            return TurnResult(
                success=False,
                error_code=err.code.value,
                error_message=err.message,
            )

    async def _commit_turn_inner(self, *, request_id: str, message: str) -> TurnResult:
        # Early validation (before any provider call): mirrors the
        # LocalStorage request/message contract so invalid input never
        # spends a provider call.
        from backend.local_storage.storage import (
            MAX_MESSAGE_LENGTH,
            MAX_REQUEST_ID_LENGTH,
            _REQUEST_ID_ALLOWED,
        )

        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > MAX_REQUEST_ID_LENGTH
            or not all(ch in _REQUEST_ID_ALLOWED for ch in request_id)
        ):
            raise StorageValidationError("invalid_request_id", "request id invalid")
        if (
            not isinstance(message, str)
            or not message
            or len(message) > MAX_MESSAGE_LENGTH
        ):
            raise StorageValidationError("empty_message", "message invalid")

        storage = self._ensure_storage()

        # Atomic admission BEFORE any provider call (#336 review
        # blocker 4): reserve_request is an atomic insert-or-classify,
        # so two concurrent turns with the same request id cannot BOTH
        # observe fresh and spend provider calls — exactly one wins
        # the reservation, the loser learns about it deterministically.
        # A completed request with the SAME message replays the
        # persisted result; a divergent message or an in-flight
        # request is a conflict.
        pre = storage.reserve_request(request_id, message)
        if pre.status == "replay" and pre.committed is not None:
            committed = pre.committed
            return TurnResult(
                success=True,
                response=committed.response,
                emotion_state=dict(committed.emotion_state),
                message_id=committed.message_id,
                revision=committed.revision,
                replayed=True,
            )
        if pre.status == "conflict":
            raise ConflictError(
                "request_payload_conflict",
                "Request id conflicts with a different message.",
                expected_revision=0,
                request_id=request_id,
            )
        if pre.status != "reserved":
            # reserve_request only returns reserved/replay/conflict.
            raise StorageValidationError("invalid_request_id", "admission invalid")

        try:
            return await self._execute_reserved_turn(
                storage=storage,
                request_id=request_id,
                message=message,
            )
        except BaseException:
            # The turn failed AFTER admission: release the pending
            # reservation so the same request id can be retried in
            # this live session (crash recovery only runs at open
            # time). Completed rows are never touched — only pending.
            try:
                storage.release_request(request_id)
            except Exception:  # noqa: BLE001 (best-effort release)
                pass
            raise

    async def _execute_reserved_turn(
        self,
        *,
        storage,
        request_id: str,
        message: str,
    ) -> TurnResult:
        budget = TurnBudget(
            deadline=_time.monotonic() + self._turn_deadline,
            reserve=10.0,
            now_provider=_time.monotonic,
        )
        t0 = _time.monotonic()

        # 1. Load state (read-only).
        state = self._load_state()

        emotional = migrate_legacy_snapshot(state.emotional_state)
        relationship = migrate_legacy_relationship_snapshot(
            state.relationship_state
        )

        # 2. Load local context (read-only).
        loaded = self._load_context(state, message)

        # 3. Appraisal (LLM).
        provider = self._provider_port()
        appraisal = await provider.appraise(message, budget)

        # 4. Pure-domain transitions.
        current_time = self._now()
        transition_result = transition_emotion(
            previous_state=emotional,
            appraisal=appraisal,
            current_time=current_time,
            config=self._transition_config,
        )
        new_emotional = transition_result.state
        new_relationship = transition_relationship(
            previous_state=relationship,
            appraisal=appraisal,
            current_time=current_time,
            config=self._relationship_config,
        )

        # 5. Trusted policy + envelope (pure domain).
        trusted_policy = provider.build_trusted_policy(
            new_emotional, new_relationship, ""
        )
        bundle = build_context_bundle(
            trusted_policy=trusted_policy, loaded_data=loaded
        )
        envelope = build_envelope(bundle, message)

        # 6. Generation (LLM).
        response_text = await provider.generate(envelope.messages, budget)

        # 7. Public emotion projection + atomic commit.
        public_emotion = project_public_emotion(new_emotional, appraisal)
        duration_ms = max(0, int((_time.monotonic() - t0) * 1000))
        replay_payload = {
            "response": response_text,
            "emotion_state": public_emotion.model_dump(),
            "message_id": _new_message_id(),
            "duration_ms": duration_ms,
        }

        committed = storage.commit_turn(
            request_id=request_id,
            user_message=message,
            assistant_message=response_text,
            emotional_state=new_emotional.to_dict(),
            relationship_state=new_relationship.to_dict(),
            public_response=response_text,
            replay_payload=replay_payload,
            expected_revision=state.revision,
        )
        return TurnResult(
            success=True,
            response=committed.response,
            emotion_state=dict(committed.emotion_state),
            message_id=committed.message_id,
            revision=committed.revision,
            duration_ms=duration_ms,
            replayed=False,
        )

    # ── privacy operations ────────────────────────────────────────────────

    def delete_history(self) -> dict[str, Any]:
        return self._privacy_op("delete_history")

    def delete_memories(self) -> dict[str, Any]:
        return self._privacy_op("delete_memories")

    def reset_emotional_state(self) -> dict[str, Any]:
        return self._privacy_op("reset_emotional_state")

    def reset_relationship_state(self) -> dict[str, Any]:
        return self._privacy_op("reset_relationship_state")

    def _privacy_op(self, name: str) -> dict[str, Any]:
        try:
            storage = self._ensure_storage()
            result = getattr(storage, name)()
            if not isinstance(result, dict) or result.get("status") != "applied":
                raise LocalStorageError(LocalErrorCode.STORAGE)
            return {"success": True, "result": result}
        except LocalStorageError as err:
            return {
                "success": False,
                "error_code": err.code.value,
                "error_message": err.message,
            }
        except Exception as exc:  # noqa: BLE001
            err = sanitized_error(exc)
            return {
                "success": False,
                "error_code": err.code.value,
                "error_message": err.message,
            }

    # ── internal helpers ──────────────────────────────────────────────────

    def _load_state(self):
        storage = self._ensure_storage()
        return storage.load_user_state()

    def _load_context(self, state, current_message: str) -> LoadedContextData:
        """Build the local LoadedContextData (history + memories + profile)."""
        storage = self._ensure_storage()
        history_rows = storage.load_recent_history(limit=self._history_limit)
        memories = _load_local_memories(storage)
        return LoadedContextData(
            history_rows=tuple(history_rows),
            retrieved_memories=tuple(memories),
            profile_snapshot=dict(state.user_profile),
            persona_snapshot=state.persona_config or "",
        )

    def get_storage_metrics(self) -> dict[str, Any]:
        storage = self._ensure_storage()
        return storage.storage_metrics()


def _real_epoch_clock() -> float:
    """Domain clock: epoch seconds (matches the web engine's clock)."""
    return _time.time()


def _public_emotion_from_snapshot(
    snapshot: Mapping[str, Any], timestamp: float
) -> dict[str, Any]:
    """Project a persisted snapshot into the public emotion DTO.

    Kept for the bridge's state endpoint if the UI later wants the
    projected mood; the chat turn results already carry the projection.
    """
    state = migrate_legacy_snapshot(snapshot)
    appraisal = AppraisalV1.neutral()
    return project_public_emotion(state, appraisal).model_dump()


def _new_message_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _load_local_memories(storage) -> list[_LocalContextMemory]:
    """Load approved local memories (bounded, sanitized) for context."""
    try:
        rows = storage.load_recent_memories(limit=5)
    except AttributeError:
        return []
    except Exception:  # noqa: BLE001 (memories are optional context)
        return []
    memories: list[_LocalContextMemory] = []
    for content, metadata in rows:
        try:
            memories.append(_LocalContextMemory(content=content, metadata=metadata))
        except Exception:  # noqa: BLE001 (single bad row never breaks a turn)
            continue
    return memories


class GroqRuntimeProvider(ProviderPort):
    """Adapter that speaks to Groq via ``GroqClientManager``.

    Appraisal and generation mirror the web engine's calls (models,
    temperatures, token limits, JSON mode for appraisal) with the trusted
    policy built from the same canonical template.
    """

    def __init__(self, manager: Any, provider_config: ProviderConfig | None = None) -> None:
        self._manager = manager
        self._config = provider_config or ProviderConfig()
        self._presentation = AffectiveEngine()

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        import json as _json

        messages = [
            {"role": "system", "content": _APPRAISAL_POLICY},
            {"role": "user", "content": message},
        ]
        validate_provider_input(messages)
        response = await self._manager.chat_completion_async(
            messages=messages,
            model=self._config.fast_model_id,
            budget=budget,
            stage="appraisal",
            temperature=0,
            max_tokens=self._config.appraisal_max_output_tokens,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw or not isinstance(raw, str) or not raw.strip():
            raise _invalid_response("Empty appraisal response.")
        try:
            payload = _json.loads(raw)
        except _json.JSONDecodeError:
            raise _invalid_response("Invalid JSON from appraisal.") from None
        parse_result = parse_llm_appraisal(payload)
        if parse_result.is_fallback:
            raise _invalid_response("Invalid appraisal.")
        return parse_result.appraisal

    async def generate(self, messages: list, budget: TurnBudget) -> str:
        validate_provider_input(messages)
        response = await self._manager.chat_completion_async(
            messages=messages,
            model=self._config.main_model_id,
            budget=budget,
            stage="generation",
            temperature=0.8,
            max_tokens=self._config.main_max_output_tokens,
        )
        content = response.choices[0].message.content
        if not content or not isinstance(content, str) or not content.strip():
            raise _invalid_response("Empty generation response.")
        return content

    def build_trusted_policy(
        self,
        emotional_state: EmotionalStateV1,
        relationship: RelationshipStateV1,
        adaptation_strategy: str = "",
    ) -> str:
        acting = self._presentation.get_acting_instruction(_to_legacy_state(emotional_state))
        mood = self._presentation.get_emotional_label(_to_legacy_state(emotional_state))
        policy = _TRUSTED_POLICY_TEMPLATE.format(
            mood_label=mood,
            pleasure=emotional_state.pleasure,
            arousal=emotional_state.arousal,
            dominance=emotional_state.dominance,
            libido=emotional_state.libido,
            aggression=emotional_state.aggression,
            connection=emotional_state.connection,
            bond_label=compute_bond_label(relationship),
            trust=relationship.trust,
            affection=relationship.affection,
            tension=relationship.tension,
            acting_instruction=acting,
            adaptation_strategy=adaptation_strategy or "Seja você mesma.",
        )
        return policy.strip() + BOUNDARY_RULE


def _invalid_response(message: str) -> TurnExecutionError:
    from backend.turn_execution import TurnErrorCode

    return TurnExecutionError(TurnErrorCode.provider_invalid_response, message)


def _to_legacy_state(state: EmotionalStateV1):
    """Adapt ``EmotionalStateV1`` to the light ``EmotionalState`` used by
    :class:`AffectiveEngine` presentation methods."""
    from backend.emotional_core import EmotionalState

    return EmotionalState(
        pleasure=state.pleasure,
        arousal=state.arousal,
        dominance=state.dominance,
        libido=state.libido,
        aggression=state.aggression,
        connection=state.connection,
        energy=state.energy,
        tension=state.tension,
        coping_mode=state.coping_mode,
        last_update=state.timestamp,
    )


def build_groq_runtime_provider(
    keys: list[str] | None = None,
    groq_params: GroqCallParams | None = None,
) -> GroqRuntimeProvider:
    """Build the real remote provider (Groq) for the desktop runtime.

    Keys are read on the Python side only (never in the Vite bundle,
    never through the bridge). Raises ``GroqConfigurationError`` when no
    key is configured — the caller maps that to the sanitized
    ``configuration`` code.
    """
    from backend.groq_manager import GroqClientManager

    manager = GroqClientManager(
        keys=keys,
        groq_params=groq_params or GroqCallParams(),
    )
    return GroqRuntimeProvider(manager)


def build_companion_runtime(
    storage_path: Path | str | None = None,
    *,
    provider: ProviderPort | None = None,
) -> CompanionRuntime:
    """Build the production runtime with the default local database path."""
    if storage_path is None:
        from backend.local_storage.storage import default_database_path

        storage_path = default_database_path()
    return CompanionRuntime(
        storage_path=storage_path,
        provider=provider,
    )
