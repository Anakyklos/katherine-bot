import os
import logging
from datetime import datetime, UTC
from supabase import create_client, Client
from sentence_transformers import SentenceTransformer
import time
from typing import Optional, Callable
from .relationship import RelationshipStateV1
from .emotional_domain import EmotionalStateV1
from .archival_memory import PersistedTurnRef, ArchivalExtractionEnvelope, ArchivalDuplicateError

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

    # Use the provided timeout (must come from validated TurnExecutionConfig).
    # If None, default to 5.0 — no env-var re-parsing here.
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
        # 1. Initialize Supabase with timeout config
        # The timeout comes from validated TurnExecutionConfig; if not provided,
        # the factory will parse from env (with fallback to 5.0s default).
        if supabase_factory is not None:
            self.supabase: Optional[Client] = supabase_factory()
        else:
            # Use the config's supabase_timeout, or None (let factory handle it)
            if supabase_timeout is not None:
                factory = lambda: _default_supabase_factory(supabase_timeout)
            else:
                factory = lambda: _default_supabase_factory()
            self.supabase: Optional[Client] = factory()

        # 2. Initialize Embeddings Model (Local)
        try:
            self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
        except Exception:
            self.embedding_model = None

        # 3. Clock for deterministic testing
        self._clock = clock



    def load_user_state(self, user_id: str, default_timestamp: float | None = None) -> dict:
        """
        Loads the full user state from Supabase 'profiles' table.
        If not found, creates a default profile using the given ``default_timestamp``
        (or ``self._clock()`` if not provided).
        Raises StateLoadError on transient database failures.
        """
        if not self.supabase:
            raise StateLoadError("Serviço de persistência indisponível.")

        try:
            response = self.supabase.table("profiles").select("*").eq("user_id", user_id).execute()

            if response is None:
                raise StateLoadError("Sem resposta do banco de dados na leitura.")

            # Check for error in response if client supports it
            if hasattr(response, 'error') and response.error:
                raise StateLoadError("Erro retornado pelo banco de dados na leitura.")

            if not hasattr(response, 'data') or response.data is None:
                raise StateLoadError("Resposta inválida do banco de dados na leitura.")

            if len(response.data) == 0:
                # Create default profile with the provided timestamp or clock
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
        effective_timestamp = (
            timestamp if timestamp is not None else self._clock()
        )
        v1_state = EmotionalStateV1.neutral(timestamp=effective_timestamp)
        return {
            "persona_config": "Katherine...",
            "user_profile": {},
            "relationship_state": RelationshipStateV1.neutral(timestamp=effective_timestamp).to_dict(),
            "emotional_state": v1_state.to_dict()
        }

    def sync_state(self, user_id: str, emotional_state: EmotionalStateV1, relationship: RelationshipStateV1, user_profile: dict = None):
        """
        Persists the current state to Supabase.
        Accepts only ``EmotionalStateV1`` and ``RelationshipStateV1`` (serialized via
        ``.to_dict()`` for JSONB). Raises ``StatePersistenceError`` on invalid type
        or database failure.
        """
        if not isinstance(emotional_state, EmotionalStateV1):
            raise StatePersistenceError(
                "emotional_state must be an EmotionalStateV1 instance."
            )

        if not isinstance(relationship, RelationshipStateV1):
            raise StatePersistenceError(
                "relationship must be a RelationshipStateV1 instance."
            )

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

            if response is None:
                raise StatePersistenceError()

            if hasattr(response, 'error') and response.error:
                raise StatePersistenceError()

            # Check for zero updated rows (response.data is empty list)
            if not response.data:
                raise StatePersistenceError()

        except StatePersistenceError:
            raise
        except Exception:
            raise StatePersistenceError() from None

    def load_recent_history(self, user_id: str, limit: int = 10) -> list:
        if not self.supabase:
            raise ContextLoadError("Serviço de persistência indisponível.")
        try:
            response = self.supabase.table("chat_logs")\
                .select("role, content")\
                .eq("user_id", user_id)\
                .order("created_at", desc=True)\
                .order("id", desc=True)\
                .limit(limit)\
                .execute()
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
                role = item["role"]
                content = item["content"]
                if role not in ("user", "assistant"):
                    raise ContextLoadError("Role inválida no histórico recente.")
                if not isinstance(content, str):
                    raise ContextLoadError("Conteúdo da mensagem não é uma string.")
                if len(content) > MAX_MESSAGE_LENGTH:
                    raise ContextLoadError("Mensagem no histórico excede o limite máximo de caracteres permitido.")
                
                # Normalize: keep only role and content
                normalized.append({
                    "role": role,
                    "content": content
                })

            return normalized[::-1]
        except ContextLoadError as e:
            logger.error(f"Erro ao carregar histórico: {type(e).__name__}")
            raise
        except Exception as e:
            logger.error(f"Erro ao carregar histórico: {type(e).__name__}")
            raise ContextLoadError("Falha ao carregar histórico de conversação.") from None

    def get_context(self, user_id: str, current_message: str, user_state: dict):
        history = self.load_recent_history(user_id, limit=10)
        short_term_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
        relevant_memories = self._retrieve_relevant(user_id, current_message)
        context_str = f"""
        === CORE MEMORY (QUEM VOCÊ É) ===
        {user_state.get('persona_config', 'Katherine...')}

        === CORE MEMORY (QUEM É O USUÁRIO) ===
        {user_state.get('user_profile', {})}

        === MEMÓRIA ARQUIVADA (LEMBRANÇAS RELEVANTES) ===
        {relevant_memories}

        === CONVERSA ATUAL (CURTO PRAZO) ===
        {short_term_str}
        """
        return context_str

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
            
            return PersistedTurnRef(
                user_id=user_id,
                source_chat_log_id=user_rec["id"],
                assistant_chat_log_id=assistant_rec["id"]
            )
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
            response = self.supabase.table("chat_logs")\
                .select("id, user_id, role, content")\
                .eq("id", source_chat_log_id)\
                .eq("user_id", user_id)\
                .execute()
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
        
        facts_data = [
            {
                "content": fact.content,
                "importance": fact.importance,
                "tags": fact.tags
            }
            for fact in envelope.facts
        ]

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


    def _retrieve_relevant(self, user_id: str, query: str):
        if not self.supabase or not self.embedding_model:
            return "Memória indisponível (offline)."

        try:
            # Generate embedding
            query_embedding = self.embedding_model.encode(query).tolist()

            # Call RPC function
            params = {
                "query_embedding": query_embedding,
                "match_threshold": 0.5,
                "match_count": 3,
                "filter_user_id": user_id
            }
            response = self.supabase.rpc("match_memories", params).execute()

            if not response.data:
                return "Nenhuma memória específica encontrada."

            formatted = []
            for doc in response.data:
                meta = doc.get('metadata', {})
                formatted.append(f"- {doc['content']} (Tags: {meta.get('tags', '')})")

            return "\n".join(formatted)
        except Exception:
            return ""
