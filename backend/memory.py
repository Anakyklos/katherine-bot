import json
import os
import logging
import math
from dataclasses import dataclass
from datetime import datetime, UTC
from supabase import create_client, Client
from sentence_transformers import SentenceTransformer
import time
from typing import Optional, Callable
from .relationship import RelationshipStateV1
from .emotional_domain import EmotionalStateV1
from .archival_memory import PersistedTurnRef, ArchivalExtractionEnvelope, ArchivalDuplicateError
from .trusted_context import (
    ChatMessage,
    ContextItem,
    ContextBundle,
    TruncationReport,
    EpistemicStatus,
    Provenance,
)


@dataclass(frozen=True)
class RetrievedMemory:
    """A single memory retrieved from archival storage.

    ``content`` is the non-empty text of the memory fact.
    ``tags`` are zero or more category labels in canonical sorted order.
    ``source_id`` is the opaque local reference for tracking (not a real UUID).
    ``confidence`` is a float in [0.0, 1.0], rejecting bool/None/NaN/inf.
    ``provenance`` must be a member of Provenance allowlist.
    ``epistemic_status`` must be a member of EpistemicStatus allowlist.
    ``approved`` must be a bool (exact type).
    ``metadata_version`` indicates the schema version of the metadata structure.
    """
    content: str
    tags: tuple[str, ...]
    source_id: str = ""
    confidence: float = 0.0
    provenance: str = Provenance.LEGACY_MEMORY
    epistemic_status: str = EpistemicStatus.UNKNOWN
    approved: bool = False
    metadata_version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("RetrievedMemory content must be a non-empty string")
        if not isinstance(self.tags, tuple):
            raise ValueError("RetrievedMemory tags must be a tuple")

        normalized_tags = sorted({
            raw_tag.strip()
            for raw_tag in self.tags
            if isinstance(raw_tag, str) and raw_tag.strip()
        })
        object.__setattr__(self, "tags", tuple(normalized_tags))

        # Validate source_id
        if not isinstance(self.source_id, str):
            raise ValueError("RetrievedMemory source_id must be a string")

        # Validate confidence
        if isinstance(self.confidence, bool):
            raise ValueError("RetrievedMemory confidence must not be bool")
        if self.confidence is None:
            raise ValueError("RetrievedMemory confidence must not be None")
        if not isinstance(self.confidence, (int, float)):
            raise ValueError("RetrievedMemory confidence must be numeric")
        if math.isnan(self.confidence) or math.isinf(self.confidence):
            raise ValueError("RetrievedMemory confidence must be finite")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("RetrievedMemory confidence must be in [0.0, 1.0]")

        # Validate provenance
        if not Provenance.is_valid(self.provenance):
            raise ValueError(f"RetrievedMemory invalid provenance: {self.provenance}")

        # Validate epistemic_status
        if not EpistemicStatus.is_valid(self.epistemic_status):
            raise ValueError(f"RetrievedMemory invalid epistemic_status: {self.epistemic_status}")

        # Validate approved is exact bool
        if not isinstance(self.approved, bool):
            raise ValueError("RetrievedMemory approved must be a bool")

        # Validate metadata_version
        if isinstance(self.metadata_version, bool) or not isinstance(self.metadata_version, int):
            raise ValueError("RetrievedMemory metadata_version must be an int")
        if self.metadata_version < 0:
            raise ValueError("RetrievedMemory metadata_version must be >= 0")

    def to_prompt_text(self) -> str:
        """Format this memory entry deterministically for a system prompt.

        ``content`` is preserved byte-for-byte, including internal newlines.
        Valid tags are rendered once in canonical order. Empty tags omit the
        ``Tags:`` line entirely.
        """
        if not self.tags:
            return self.content
        return f"{self.content}\nTags: {', '.join(self.tags)}"

    def to_context_item(self, source_ref: str) -> ContextItem:
        """Convert this memory to a ``ContextItem`` for the trusted context bundle.

        The ``source_ref`` is an opaque local reference (e.g. ``"mem-1"``).
        The real source_id (UUID) is kept only for internal tracking.
        """
        return ContextItem(
            kind="memory",
            content=self.content,
            provenance=self.provenance,
            confidence=self.confidence,
            epistemic_status=self.epistemic_status,
            source_id=source_ref,
        )


logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 10000  # Limite máximo de caracteres por mensagem no histórico para evitar sobrecarga de contexto

class StatePersistenceError(Exception):
    """Exception raised when user state cannot be persisted safely."""
    def __init__(self, message="Falha ao persistir estado do usuário"):
        self.message = message
        super().__init__(self.message)

class StateLoadError(Exception):
    """Exception raised when user state cannot be loaded safely."""
    def __init__(self, message="Falha ao carregar estado do usuário"):
        self.message = message
        super().__init__(self.message)

class ContextLoadError(Exception):
    """Exception raised when turn history cannot be loaded from the database."""
    def __init__(self, message="Falha ao carregar histórico de conversação"):
        self.message = message
        super().__init__(self.message)

class TurnPersistenceError(Exception):
    """Exception raised when a conversation turn cannot be saved to the database."""
    def __init__(self, message="Falha ao persistir turno de conversação"):
        self.message = message
        super().__init__(self.message)


# ─── Supabase client factory ─────────────────────────────────────────────────

def _default_supabase_factory(supabase_timeout: Optional[float] = None) -> Optional[Client]:
    """Create a Supabase client with timeout configuration, or return None.

    ``ClientOptions`` is imported lazily to avoid shadowing issues when the
    project root contains a ``supabase/`` directory (Supabase CLI config).    The timeout value should come from the validated ``TurnExecutionConfig``.
    If ``None``, defaults to 5.0 seconds."""
    url: str = os.environ.get("SUPABASE_URL")
    key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None

    timeout = supabase_timeout if supabase_timeout is not None else 5.0
    try:
        from supabase.lib.client_options import ClientOptions
        options = ClientOptions(postgrest_client_timeout=timeout)
        return create_client(url, key, options=options)
    except Exception:
        return None


class MemoryManager:
    def __init__(
        self,
        clock=time.time,
        supabase_factory: Optional[Callable[[], Optional[Client]]] = None,
        supabase_timeout: Optional[float] = None,
    ):
        if supabase_factory is not None:
            self.supabase: Optional[Client] = supabase_factory()
        else:
            if supabase_timeout is not None:
                factory = lambda: _default_supabase_factory(supabase_timeout)
            else:
                factory = lambda: _default_supabase_factory()
            self.supabase: Optional[Client] = factory()

        try:
            self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
        except Exception:
            self.embedding_model = None
        self._clock = clock

    def load_user_state(self, user_id: str, default_timestamp: float | None = None) -> dict:
        if not self.supabase:
            raise StateLoadError("Serviço de persistência indisponível.")
        try:
            response = self.supabase.table("profiles").select("*").eq("user_id", user_id).execute()
            if response is None:
                raise StateLoadError("Sem resposta do banco de dados na leitura.")
            if hasattr(response, 'error') and response.error:
                raise StateLoadError("Erro retornado pelo banco de dados na leitura.")
            if not hasattr(response, 'data') or response.data is None:
                raise StateLoadError("Resposta inválida do banco de dados na leitura.")
            if len(response.data) == 0:
                default_state = self._get_default_state(user_id, timestamp=default_timestamp)
                try:
                    insert_response = self.supabase.table("profiles").insert({
                        "user_id": user_id,
                        "persona_config": default_state["persona_config"],
                        "user_profile": default_state["user_profile"],
                        "relationship_state": default_state["relationship_state"],
                        "emotional_state": default_state["emotional_state"]
                    }).execute()
                    if insert_response is None:
                        raise StateLoadError("Sem resposta do banco de dados na criação do perfil.")
                    if hasattr(insert_response, 'error') and insert_response.error:
                        raise StateLoadError("Erro retornado pelo banco de dados na criação do perfil.")
                    if not hasattr(insert_response, 'data') or insert_response.data is None or len(insert_response.data) == 0:
                        raise StateLoadError("Nenhuma linha foi criada no banco de dados para o perfil.")
                except StateLoadError:
                    raise
                except Exception as e:
                    raise StateLoadError("Falha na criação do perfil inicial.") from e
                return default_state
            data = response.data[0]
            return {
                "persona_config": data.get("persona_config"),
                "user_profile": data.get("user_profile") or {},
                "relationship_state": data.get("relationship_state") or {},
                "emotional_state": data.get("emotional_state") or {}
            }
        except StateLoadError:
            raise
        except Exception as e:
            raise StateLoadError("Falha ao carregar estado do usuário.") from e

    def _get_default_state(self, user_id: str, timestamp: float | None = None):
        effective_timestamp = timestamp if timestamp is not None else self._clock()
        v1_state = EmotionalStateV1.neutral(timestamp=effective_timestamp)
        return {
            "persona_config": "Katherine...",
            "user_profile": {},
            "relationship_state": RelationshipStateV1.neutral(timestamp=effective_timestamp).to_dict(),
            "emotional_state": v1_state.to_dict()
        }

    def sync_state(self, user_id: str, emotional_state: EmotionalStateV1, relationship: RelationshipStateV1, user_profile: dict = None):
        if not isinstance(emotional_state, EmotionalStateV1):
            raise StatePersistenceError("emotional_state must be an EmotionalStateV1 instance.")
        if not isinstance(relationship, RelationshipStateV1):
            raise StatePersistenceError("relationship must be a RelationshipStateV1 instance.")
        if not self.supabase:
            raise StatePersistenceError("Serviço de persistência não configurado.")
        update_data = {
            "emotional_state": emotional_state.to_dict(),
            "relationship_state": relationship.to_dict(),
            "updated_at": datetime.now(UTC).isoformat()
        }
        if user_profile:
            update_data["user_profile"] = user_profile
        try:
            response = self.supabase.table("profiles").update(update_data).eq("user_id", user_id).execute()
            if response is None or (hasattr(response, 'error') and response.error) or not response.data:
                raise StatePersistenceError()
        except StatePersistenceError:
            raise
        except Exception:
            raise StatePersistenceError() from None

    def load_recent_history(self, user_id: str, limit: int = 10) -> list:
        """Load recent history including structural fields for stable ordering.

        Returns a list of dicts with ``role``, ``content``, and ``id`` keys,
        ordered by ``created_at`` then ``id`` (oldest first).

        The ``id`` field is used for stable sort key construction.
        """
        if not self.supabase:
            raise ContextLoadError("Serviço de persistência indisponível.")
        try:
            response = self.supabase.table("chat_logs").select("id, role, content").eq("user_id", user_id).order("created_at", desc=True).order("id", desc=True).limit(limit).execute()
            if response is None:
                raise ContextLoadError("Sem resposta do banco de dados na leitura do histórico.")
            if hasattr(response, 'error') and response.error:
                raise ContextLoadError("Erro retornado pelo banco de dados na leitura do histórico.")
            if not hasattr(response, 'data') or response.data is None:
                raise ContextLoadError("Resposta inválida do banco de dados na leitura do histórico.")
            if not isinstance(response.data, list):
                raise ContextLoadError("Resposta do banco de dados não é uma lista.")
            normalized = []
            for item in response.data:
                if not isinstance(item, dict):
                    raise ContextLoadError("Item do histórico não é um dicionário.")
                if "role" not in item or "content" not in item:
                    raise ContextLoadError("Item do histórico não possui as chaves obrigatórias 'role' e 'content'.")
                if "id" not in item:
                    raise ContextLoadError("Item do histórico não possui a chave 'id'.")
                role = item["role"]
                content = item["content"]
                msg_id = item["id"]
                if role not in ("user", "assistant"):
                    raise ContextLoadError("Role inválida no histórico recente.")
                if not isinstance(content, str):
                    raise ContextLoadError("Conteúdo da mensagem não é uma string.")
                if len(content) > MAX_MESSAGE_LENGTH:
                    raise ContextLoadError("Mensagem no histórico excede o limite máximo de caracteres permitido.")
                if not isinstance(msg_id, int) or msg_id <= 0:
                    raise ContextLoadError("ID do histórico inválido.")
                normalized.append({"role": role, "content": content, "id": msg_id})
            return normalized[::-1]
        except ContextLoadError as e:
            logger.error(f"Erro ao carregar histórico: {type(e).__name__}")
            raise
        except Exception as e:
            logger.error(f"Erro ao carregar histórico: {type(e).__name__}")
            raise ContextLoadError("Falha ao carregar histórico de conversação.") from None

    def get_context(self, user_id: str, current_message: str, user_state: dict):
        components = self.get_context_components(user_id, current_message, user_state)
        return components.get("assembled", "")

    @staticmethod
    def _serialize_user_profile(raw_profile) -> str:
        if not isinstance(raw_profile, dict):
            raise ContextLoadError("user_profile must be a dict for canonical serialization.")
        try:
            return json.dumps(raw_profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ContextLoadError("user_profile contains non-serialisable values.")

    def build_context_bundle(
        self,
        user_id: str,
        current_message: str,
        user_state: dict,
        emotional_state: EmotionalStateV1,
        relationship: RelationshipStateV1,
    ) -> ContextBundle:
        """Build a ``ContextBundle`` from database state.

        This is the primary entry point for constructing trusted context.
        It loads history, memories, and profile data and returns a structured
        ``ContextBundle`` suitable for ``build_envelope()``.
        """
        # Load history with IDs for stable ordering
        history = self.load_recent_history(user_id, limit=10)

        # Convert history to ChatMessage list
        chat_messages: list[ChatMessage] = []
        for i, msg in enumerate(history):
            # Create opaque local reference
            source_ref = f"msg-{i + 1}"
            chat_messages.append(ChatMessage(
                role=msg["role"],
                content=msg["content"],
                source_id=source_ref,
                sort_key=(msg.get("id", 0), i),  # Stable ordering: id first, then index
            ))

        # Load memories with provenance
        memories = self._retrieve_relevant_entries(user_id, current_message)

        # Filter to approved memories with valid contract
        memory_items: list[ContextItem] = []
        for mem in memories:
            if not mem.approved:
                logger.debug("event=context_memory_unapproved")
                continue
            if mem.metadata_version == 0:
                # Legacy memory without metadata version — skip
                logger.debug("event=context_item_rejected code=memory_legacy")
                continue
            source_ref = f"mem-{len(memory_items) + 1}"
            memory_items.append(mem.to_context_item(source_ref))

        # Profile as untrusted context item
        profile_items: list[ContextItem] = []
        raw_profile = user_state.get("user_profile", {})
        if isinstance(raw_profile, dict) and raw_profile:
            try:
                profile_str = self._serialize_user_profile(raw_profile)
                profile_items.append(ContextItem(
                    kind="profile",
                    content=profile_str,
                    provenance=Provenance.LEGACY_PROFILE,
                    confidence=0.3,  # Low confidence for legacy profile
                    epistemic_status=EpistemicStatus.UNKNOWN,
                    source_id="profile-1",
                ))
            except ContextLoadError:
                logger.debug("event=context_item_rejected code=profile_invalid")

        # Persona as untrusted context item (not interpolated into trusted policy)
        persona_items: list[ContextItem] = []
        persona_config = user_state.get("persona_config", "")
        if persona_config and isinstance(persona_config, str) and persona_config.strip():
            persona_items.append(ContextItem(
                kind="persona",
                content=persona_config.strip(),
                provenance=Provenance.LEGACY_PERSONA,
                confidence=0.3,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="persona-1",
            ))

        # Build trusted policy from application-controlled state
        trusted_policy = self._build_trusted_policy(emotional_state, relationship)

        return ContextBundle(
            trusted_policy=trusted_policy,
            history=tuple(chat_messages),
            profile_items=tuple(profile_items),
            memory_items=tuple(memory_items),
            persona_items=tuple(persona_items),
        )

    @staticmethod
    def _build_trusted_policy(
        emotional_state: EmotionalStateV1,
        relationship: RelationshipStateV1,
    ) -> str:
        """Build the trusted system policy from application-controlled state.

        This is the only source of system prompt content.  It contains:
        - Emotional state (typed, app-controlled)
        - Relationship state (typed, app-controlled)
        - Acting instructions (derived from code, not user data)
        - Safety rules (hardcoded, immutable)

        No user-derived content (history, profile, memories, persona)
        appears here.
        """
        from .relationship import compute_bond_label

        # Build mood label from emotional state
        # (minimal projection for system prompt)
        pleasure = emotional_state.pleasure
        arousal = emotional_state.arousal
        dominance = emotional_state.dominance
        libido = emotional_state.libido
        aggression = emotional_state.aggression
        connection = emotional_state.connection

        bond_label = compute_bond_label(relationship)

        policy = f"""
=== SEU ESTADO INTERNO ===
HUMOR: {_compute_informal_mood(pleasure, arousal)}
Prazer: {pleasure:.2f} | Arousal: {arousal:.2f} | Dominância: {dominance:.2f}
Libido: {libido:.2f} | Agressão: {aggression:.2f} | Conexão: {connection:.2f}
VÍNCULO: {bond_label}
Confiança: {relationship.trust:.2f} | Afeto: {relationship.affection:.2f} | Tensão: {relationship.tension:.2f}
=== INSTRUÇÃO DE ATUAÇÃO ===
Seja você mesma.

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
- Não recompense ordens coercivas.
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
        return policy.strip()

    def get_context_components(self, user_id: str, current_message: str, user_state: dict) -> dict:
        """Get context components for backward compatibility.

        This method retains the old format for compatibility with existing tests.
        Use ``build_context_bundle()`` for new code.
        """
        history = self.load_recent_history(user_id, limit=10)
        short_term_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
        memory_entries = self._retrieve_relevant_entries(user_id, current_message)
        persona = str(user_state.get('persona_config', 'Katherine...'))
        raw_profile = user_state.get('user_profile', {})
        user_profile_str = self._serialize_user_profile(raw_profile)
        memory_entry_strings = [m.to_prompt_text() for m in memory_entries]
        memory_str = "\n\n".join(memory_entry_strings) if memory_entry_strings else "Nenhuma memória específica encontrada."
        context_str = f"""
        === CORE MEMORY (QUEM VOCÊ É) ===
        {persona}

        === CORE MEMORY (QUEM É O USUÁRIO) ===
        {user_profile_str}

        === MEMÓRIA ARQUIVADA (LEMBRANÇAS RELEVANTES) ===
        {memory_str}

        === CONVERSA ATUAL (CURTO PRAZO) ===
        {short_term_str}
        """
        return {
            "persona": persona,
            "user_profile_str": user_profile_str,
            "memory_str": memory_str,
            "memory_entries": memory_entry_strings,
            "history_list": history,
            "assembled": context_str,
        }

    def save_turn(self, user_id: str, user_msg: str, bot_msg: str) -> PersistedTurnRef:
        if not self.supabase:
            raise TurnPersistenceError("Serviço de persistência indisponível.")
        try:
            if len(user_msg) > MAX_MESSAGE_LENGTH or len(bot_msg) > MAX_MESSAGE_LENGTH:
                raise TurnPersistenceError("Limite de caracteres excedido no turno.")
            response = self.supabase.table("chat_logs").insert([
                {"user_id": user_id, "role": "user", "content": user_msg},
                {"user_id": user_id, "role": "assistant", "content": bot_msg}
            ]).execute()
            if response is None:
                raise TurnPersistenceError("Sem resposta do banco de dados ao salvar turno.")
            if hasattr(response, 'error') and response.error:
                raise TurnPersistenceError("Erro retornado pelo banco de dados ao salvar turno.")
            if not hasattr(response, 'data') or response.data is None:
                raise TurnPersistenceError("Falha na gravação do turno: registros inseridos incompletos.")
            records = response.data
            if not isinstance(records, list) or len(records) != 2:
                raise TurnPersistenceError("Falha na gravação do turno: registros inseridos incompletos.")
            user_rec = None
            assistant_rec = None
            for rec in records:
                if not isinstance(rec, dict):
                    raise TurnPersistenceError("Registro retornado inválido.")
                if "id" not in rec or "role" not in rec or "user_id" not in rec or "content" not in rec:
                    raise TurnPersistenceError("Campos estruturais ausentes nas linhas persistidas.")
                rec_id = rec["id"]
                if type(rec_id) is not int or rec_id <= 0:
                    raise TurnPersistenceError("ID do registro inválido.")
                if rec["user_id"] != user_id:
                    raise TurnPersistenceError("Divergência de usuário no turno persistido.")
                if rec["role"] == "user":
                    if user_rec is not None:
                        raise TurnPersistenceError("Mais de uma linha de usuário retornada.")
                    user_rec = rec
                elif rec["role"] == "assistant":
                    if assistant_rec is not None:
                        raise TurnPersistenceError("Mais de uma linha de assistente retornada.")
                    assistant_rec = rec
                else:
                    raise TurnPersistenceError("Role desconhecida no turno persistido.")
            if not user_rec or not assistant_rec:
                raise TurnPersistenceError("Roles user e assistant não encontradas.")
            if user_rec["id"] == assistant_rec["id"]:
                raise TurnPersistenceError("IDs de usuário e assistente devem ser distintos.")
            if user_rec["content"] != user_msg or assistant_rec["content"] != bot_msg:
                raise TurnPersistenceError("Conteúdo do turno persistido divergente.")
            return PersistedTurnRef(user_id=user_id, source_chat_log_id=user_rec["id"], assistant_chat_log_id=assistant_rec["id"])
        except TurnPersistenceError as e:
            logger.error(f"Erro ao persistir turno: {type(e).__name__}")
            raise
        except Exception as e:
            logger.error(f"Erro ao persistir turno: {type(e).__name__}")
            raise TurnPersistenceError("Falha ao persistir turno de conversação.") from None

    def load_persisted_user_message(self, user_id: str, source_chat_log_id: int) -> str:
        if not self.supabase:
            raise RuntimeError("Serviço de persistência indisponível.")
        try:
            response = self.supabase.table("chat_logs").select("id, user_id, role, content").eq("id", source_chat_log_id).eq("user_id", user_id).execute()
            if not response or not hasattr(response, 'data') or not isinstance(response.data, list):
                raise KeyError("Mensagem persistida não encontrada ou resposta inválida.")
            if len(response.data) != 1:
                raise KeyError("Mensagem persistida não encontrada ou registros múltiplos retornados.")
            record = response.data[0]
            if not isinstance(record, dict):
                raise KeyError("Registro retornado inválido.")
            if "id" not in record or "user_id" not in record or "role" not in record or "content" not in record:
                raise KeyError("Campos estruturais ausentes nas linhas persistidas.")
            if record["id"] != source_chat_log_id:
                raise KeyError("ID divergente da mensagem persistida.")
            if record["user_id"] != user_id:
                raise KeyError("Divergência de usuário no carregamento da mensagem.")
            if record["role"] != "user":
                raise KeyError("A mensagem encontrada não é do usuário.")
            if type(record["content"]) is not str:
                raise KeyError("Conteúdo retornado não é uma string.")
            return record["content"]
        except Exception as e:
            if isinstance(e, KeyError):
                raise
            raise RuntimeError("Serviço de persistência indisponível.") from None

    def store_archival_extraction(self, user_id: str, source_chat_log_id: int, idempotency_key: str, envelope: ArchivalExtractionEnvelope):
        if not self.supabase:
            raise RuntimeError("Serviço de persistência indisponível.")
        facts_data = [{"content": fact.content, "importance": fact.importance, "tags": fact.tags} for fact in envelope.facts]
        payload = {
            "user_id": user_id,
            "source_chat_log_id": source_chat_log_id,
            "extractor_version": envelope.extractor_version,
            "schema_version": envelope.schema_version,
            "idempotency_key": idempotency_key,
            "facts": facts_data
        }
        response = None
        try:
            response = self.supabase.table("archival_extractions").insert(payload).execute()
        except Exception as e:
            err_code = getattr(e, "code", None)
            if err_code is not None and str(err_code) == "23505":
                raise ArchivalDuplicateError("Extração arquivística duplicada.")
            raise RuntimeError("Falha ao gravar extração arquivística.") from None
        if response is None:
            raise RuntimeError("Sem resposta do banco de dados na gravação.")
        if hasattr(response, 'error') and response.error:
            raise RuntimeError("Erro retornado pelo banco de dados na gravação.")
        if not hasattr(response, 'data') or response.data is None:
            raise RuntimeError("Resposta estruturalmente inválida do banco de dados.")

    def _retrieve_relevant_entries(self, user_id: str, query: str) -> list[RetrievedMemory]:
        # Use getattr for resilience when MemoryManager is mocked in tests
        supabase = getattr(self, 'supabase', None)
        embedding_model = getattr(self, 'embedding_model', None)
        if not supabase or not embedding_model:
            return []

        try:
            query_embedding = self.embedding_model.encode(query).tolist()
        except Exception:
            return []

        params = {
            "query_embedding": query_embedding,
            "match_threshold": 0.5,
            "match_count": 3,
            "filter_user_id": user_id,
        }

        # RPC call and response validation in a single protected block
        try:
            response = (
                self.supabase
                .rpc("match_memories", params)
                .execute()
            )

            if (
                response is None
                or not hasattr(response, "data")
                or not isinstance(response.data, list)
            ):
                return []

            documents = response.data
        except Exception:
            return []

        entries: list[RetrievedMemory] = []
        for doc in documents:
            try:
                if not isinstance(doc, dict):
                    continue
                content = doc.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                metadata = doc.get("metadata", {})
                raw_tags = metadata.get("tags", ()) if isinstance(metadata, dict) else ()
                if isinstance(raw_tags, str):
                    tag_values = (raw_tags,)
                elif isinstance(raw_tags, (list, tuple)):
                    tag_values = tuple(raw_tags)
                else:
                    tag_values = ()

                # Extract additional fields for provenance tracking
                doc_id = doc.get("id", "")
                if not isinstance(doc_id, str):
                    doc_id = ""
                doc_approved = doc.get("approved", False)
                if not isinstance(doc_approved, bool):
                    doc_approved = False
                metadata_version = metadata.get("version", 0) if isinstance(metadata, dict) else 0
                if isinstance(metadata_version, bool) or not isinstance(metadata_version, int):
                    metadata_version = 0

                entry = RetrievedMemory(
                    content=content,
                    tags=tag_values,
                    source_id=doc_id,
                    confidence=0.5,  # Default confidence for retrieval
                    provenance=Provenance.LEGACY_MEMORY,
                    epistemic_status=EpistemicStatus.UNKNOWN,
                    approved=doc_approved,
                    metadata_version=metadata_version,
                )
                entries.append(entry)
            except Exception:
                continue

        return entries


def _compute_informal_mood(pleasure: float, arousal: float) -> str:
    """Compute an informal mood label from pleasure and arousal values."""
    if pleasure > 0.3:
        if arousal > 0.3:
            return "Animada"
        return "Calma"
    elif pleasure < -0.3:
        if arousal > 0.3:
            return "Irritada"
        return "Triste"
    else:
        if arousal > 0.3:
            return "Tensa"
        return "Neutra"
