"""
Integration tests for backend/emotional_domain production flow — issue #234.

Coverage map (43 behavioural requirements):
─────────────────────────────────────────────────────────────────────────────
 1. Um turno válido produz exatamente um AppraisalV1.
 2. parse_llm_appraisal() é executado exatamente uma vez.
 3. transition() é executado exatamente uma vez.
 4. AffectiveEngine.update_state() não é executado.
 5. OCCAppraisal.evaluate() não é executado.
 6. _normalize_perception() não participa do caminho ativo.
 7. Relacionamento recebe adaptação derivada do AppraisalV1.
 8. Appraisal inválido usa fallback neutro.
 9. Fallback registra somente evento e código sanitizado.
10. Fallback não registra payload, prompt ou exceção.
11. Snapshot legado válido é migrado e utilizado na transição.
12. Snapshot legado migrado é persistido como v1.
13. Snapshot v1 válido carregado sem migração destrutiva.
14. Versão desconhecida falha fechado.
15. Snapshot incompleto/com campos extras falha fechado.
16. Falha no load ocorre antes de Groq/transição/persistência.
17. Novo perfil é criado com snapshot v1 desde o início.
18. sync_state() persiste to_dict() v1 (JSONB dict, not JSON string).
19. Snapshot persistido contém schema_version e timestamp, sem last_update.
20. Resposta pública é um ``EmotionStateResponse``, não um dict.
21. Resposta pública contém os campos obrigatórios do contrato v1.
22. Resposta pública não contém campos internos (acting_instruction, coping_mode, etc.).
23. Falha de persistência não retorna sucesso.
24. Falha de persistência não agenda extração arquivística.
25. Extração arquivística agendada somente após turno e estado persistidos.
26. sync_state() rejeita EmotionalStateV1 inválido com StatePersistenceError.
27. Engine e MemoryManager aceitam relógio injetável (clock=...).
28. Novos testes não usam relógio real (clock=1_700_000_000.0).
29. Spies confirmam exatamente 1 parse_llm_appraisal e 1 transition por turno.
30. Relacionamento recebe adaptação do appraisal, nunca payload bruto.
31. Snapshots inválidos bloqueiam todo o fluxo antes de efeitos externos.
32. Nenhum teste usa Groq, Supabase, embeddings ou rede reais.
33. Toda a suíte backend existente continua passando.
34. Frontend audit, tests, lint e build continuam verdes.
35. CI completa passa.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from backend.engine import ConversationEngine
from backend.memory import (
    MemoryManager,
    StateLoadError,
    StatePersistenceError,
)
from backend.emotional_domain import (
    AppraisalV1,
    EmotionalDomainError,
    EmotionalStateV1,
    migrate_legacy_snapshot,
    parse_llm_appraisal,
    transition,
)
from backend.emotion_presentation import EmotionStateResponse
from backend.relationship import RelationshipStateV1, compute_bond_label
from backend.turn_execution import TurnExecutionError, TurnErrorCode


# ─── Fixed clock ─────────────────────────────────────────────────────────────
FIXED_CLOCK = 1_700_000_000.0


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _legacy_emotion_dict(pleasure=0.0, arousal=0.0, dominance=0.0) -> dict:
    return {
        "pleasure": pleasure,
        "arousal": arousal,
        "dominance": dominance,
        "libido": 0.0,
        "aggression": 0.0,
        "connection": 0.5,
        "energy": 0.8,
        "tension": 0.0,
        "coping_mode": "HEALTHY",
        "last_update": FIXED_CLOCK,
    }


def _v1_emotion_dict(pleasure=0.0) -> dict:
    return EmotionalStateV1.create(
        pleasure=pleasure, arousal=0.0, dominance=0.0,
        libido=0.0, aggression=0.0, connection=0.5,
        energy=0.8, tension=0.0, coping_mode="HEALTHY",
        timestamp=FIXED_CLOCK,
    ).to_dict()


def _make_engine(clock=FIXED_CLOCK, archival_extraction_enabled=False):
    """Create a ConversationEngine with fixed clock and mocked external deps."""
    engine = ConversationEngine(clock=lambda: clock, archival_extraction_enabled=archival_extraction_enabled)
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": _legacy_emotion_dict(),
    })
    engine.memory_manager.sync_state = MagicMock()
    engine.memory_manager.save_turn = MagicMock()
    engine.memory_manager.get_context = MagicMock(return_value="[mocked context]")
    engine.memory_manager.load_recent_history = MagicMock(return_value=[])
    engine.memory_manager._retrieve_relevant_entries = MagicMock(return_value=[])
    from backend.trusted_context import ContextBundle, LoadedContextData
    engine.memory_manager.load_context_data = MagicMock(return_value=LoadedContextData(
        history_rows=(),
        retrieved_memories=(),
        profile_snapshot={},
        persona_snapshot="",
    ))

    # Mock sync completion for archival extraction
    sync_m = MagicMock()
    sync_m.choices = [MagicMock()]
    sync_m.choices[0].message.content = "Hi"
    engine.groq_manager.chat_completion = MagicMock(return_value=sync_m)
    
    # Mock async completion for appraisal (returns valid JSON)
    async def _mock_async_completion(**kwargs):
        async_m = MagicMock()
        async_m.choices = [MagicMock()]
        async_m.choices[0].message.content = json.dumps({
            "valence": 0.2, "arousal_shift": 0.1, "dominance_shift": 0.0,
            "triggered_emotions": {"joy": 0.5},
        })
        return async_m
    
    # Mock async completion for generation (returns valid text)
    async def _mock_generation(**kwargs):
        gen_m = MagicMock()
        gen_m.choices = [MagicMock()]
        gen_m.choices[0].message.content = "Hi there!"
        return gen_m
    
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_async_completion)
    
    return engine


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 1: sync_state() validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncStateValidation:
    """sync_state() rejects invalid emotional_state types with StatePersistenceError."""

    def _make_mm(self):
        mm = MemoryManager(clock=lambda: FIXED_CLOCK)
        mm.supabase = MagicMock()
        mock_resp = MagicMock()
        mock_resp.data = [{"user_id": "u"}]
        mock_resp.error = None
        mm.supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_resp
        return mm

    def test_accepts_emotional_state_v1(self):
        mm = self._make_mm()
        state = EmotionalStateV1.neutral(timestamp=FIXED_CLOCK)
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        mm.sync_state("u", state, rel)
        mm.supabase.table.assert_called_once_with("profiles")

    def test_rejects_legacy_emotional_state(self):
        mm = self._make_mm()
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        with pytest.raises(StatePersistenceError):
            mm.sync_state("u", "not a state", rel)

    def test_rejects_dict(self):
        mm = self._make_mm()
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        with pytest.raises(StatePersistenceError):
            mm.sync_state("u", {"pleasure": 0.0}, rel)

    def test_rejects_magic_mock(self):
        mm = self._make_mm()
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        with pytest.raises(StatePersistenceError):
            mm.sync_state("u", MagicMock(), rel)

    def test_rejects_none(self):
        mm = self._make_mm()
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        with pytest.raises(StatePersistenceError):
            mm.sync_state("u", None, rel)

    def test_invalid_type_does_not_call_db(self):
        mm = self._make_mm()
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        with pytest.raises(StatePersistenceError):
            mm.sync_state("u", "bad", rel)
        # Verify no DB call was made
        mm.supabase.table.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 2: Clock injection
# ═══════════════════════════════════════════════════════════════════════════════

class TestClockInjection:
    """Engine and MemoryManager accept injectable clock for deterministic testing."""

    def test_engine_uses_injected_clock(self):
        calls = []
        engine = ConversationEngine(clock=lambda: calls.append(1) or FIXED_CLOCK)
        assert engine._clock() == FIXED_CLOCK
        assert len(calls) == 1

    def test_memory_manager_uses_injected_clock(self):
        mm = MemoryManager(clock=lambda: FIXED_CLOCK)
        default = mm._get_default_state("u")
        ts = default["emotional_state"]["timestamp"]
        assert ts == FIXED_CLOCK

    def test_clock_propagates_to_memory_manager(self):
        engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
        assert engine.memory_manager._clock() == FIXED_CLOCK


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 3: Spy-based flow counting
# ═══════════════════════════════════════════════════════════════════════════════

class TestSpyBasedFlowCounting:
    """Spies confirm exactly one parse_llm_appraisal and one transition per turn."""

    def test_one_appraisal_and_one_transition_per_turn(self):
        async def run():
            engine = _make_engine()
            parse_spy = MagicMock(wraps=parse_llm_appraisal)
            transition_spy = MagicMock(wraps=transition)

            with patch("backend.engine.parse_llm_appraisal", parse_spy), \
                 patch("backend.engine.transition", transition_spy):

                resp, emotions = await engine.process_turn("user", "Hello")

            assert parse_spy.call_count == 1
            assert transition_spy.call_count == 1

            # Verify the appraisal passed to transition is an AppraisalV1
            parse_result = parse_spy.call_args[0][0]
            transition_kwargs = transition_spy.call_args[1]
            assert isinstance(transition_kwargs["appraisal"], AppraisalV1)
            assert isinstance(transition_kwargs["previous_state"], EmotionalStateV1)
            assert transition_kwargs["current_time"] == FIXED_CLOCK

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 4: Relationship adaptation with spy
# ═══════════════════════════════════════════════════════════════════════════════

class TestRelationshipAdaptationSpy:
    """Relationship receives adapted dict from AppraisalV1, not raw payload."""

    def test_valid_appraisal_preserves_emotions_in_relationship(self):
        """
        Cenário A — appraisal válido sem chaves top-level desconhecidas.

        O relacionamento recebe ``{valence, triggered_emotions}`` com as
        emoções válidas preservadas e nenhum campo extra.
        """
        async def run():
            engine = _make_engine()
            # The relationship manager is replaced by the pure transition function.
            # We verify that the relationship is updated via transition_relationship
            # by checking the returned state through sync_state.
            async def _mock_appraisal(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = json.dumps({
                    "valence": 0.5,
                    "arousal_shift": 0.2,
                    "dominance_shift": 0.1,
                    "triggered_emotions": {
                        "joy": 0.8,
                        "tenderness": 0.6,
                    },
                })
                return mock_resp
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_appraisal)

            await engine.process_turn("user", "Hello")

            # Verify that sync_state was called with a RelationshipStateV1
            args, _ = engine.memory_manager.sync_state.call_args
            assert len(args) >= 3
            rel = args[2]
            assert isinstance(rel, RelationshipStateV1)
            # With positive valence, trust should increase
            assert rel.trust > 0.5

        asyncio.run(run())

    def test_unknown_top_level_key_triggers_fallback_failure(self):
        """
        Cenário B — chave top-level desconhecida.

        * o parser utiliza fallback neutro;
        * o fallback na fronteira de orquestração é convertido em erro;
        * o marcador não chega ao relacionamento;
        * o marcador não aparece nos logs;
        * o turno falha com ``provider_invalid_response``.
        """
        SENSITIVE_KEY = "SENSITIVE_EXTRA_KEY_92841"

        async def run():
            engine = _make_engine()

            async def _mock_appraisal(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = json.dumps({
                    "valence": 0.5,
                    "arousal_shift": 0.2,
                    "dominance_shift": 0.1,
                    "triggered_emotions": {"joy": 0.8},
                    SENSITIVE_KEY: "should_not_leak",
                })
                return mock_resp
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_appraisal)

            logger = logging.getLogger("backend.engine")
            logger.setLevel(logging.INFO)
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger.addHandler(handler)
            try:
                with pytest.raises(TurnExecutionError) as exc_info:
                    await engine.process_turn("user", "Hello")
                assert exc_info.value.code == TurnErrorCode.provider_invalid_response
            finally:
                logger.removeHandler(handler)

            log_text = stream.getvalue()

            # No persistence calls — appraisal failure blocked the turn
            engine.memory_manager.sync_state.assert_not_called()
            engine.memory_manager.save_turn.assert_not_called()

            # Log contains sanitised fallback event, not the marker
            assert "event=emotional_appraisal_fallback" in log_text
            assert SENSITIVE_KEY not in log_text

        asyncio.run(run())

    def test_unknown_emotions_filtered_from_relationship(self):
        """Verify unknown emotions are stripped by the parser before reaching relationship."""
        async def run():
            engine = _make_engine()
            # Payload with known emotion joy=0.8 and unknown emotion_92841=0.9
            async def _mock_appraisal(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = json.dumps({
                    "valence": 0.2,
                    "arousal_shift": 0.1,
                    "dominance_shift": 0.0,
                    "triggered_emotions": {
                        "joy": 0.8,
                        "unknown_emotion_92841": 0.9,
                    },
                })
                return mock_resp
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_appraisal)

            await engine.process_turn("user", "Hello")

            args, _ = engine.memory_manager.sync_state.call_args
            rel = args[2]
            assert isinstance(rel, RelationshipStateV1)
            # Known emotion joy > 0.3 should boost affection by 0.01
            assert rel.affection == pytest.approx(0.31)
            # Valence 0.2 is not > 0.2, so trust should stay at 0.5
            # (0.2 is not strictly > 0.2, so no trust change)
            assert rel.trust == 0.5

        asyncio.run(run())

    def test_empty_appraisal_fails_with_invalid_response(self):
        """Empty appraisal dict triggers fallback in parser, which raises at orchestration boundary."""
        async def run():
            engine = _make_engine()
            async def _mock_empty_appraisal(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = json.dumps({})
                return mock_resp
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_empty_appraisal)

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

            # No persistence — appraisal failure blocked the turn
            engine.memory_manager.sync_state.assert_not_called()
            engine.memory_manager.save_turn.assert_not_called()

        asyncio.run(run())

    def test_adaptation_structure_is_clean(self):
        """The transition function receives AppraisalV1 directly (no adapter needed)."""
        from backend.relationship import transition_relationship, RelationshipTransitionConfig
        ap = AppraisalV1.create(
            valence_shift=0.3, arousal_shift=-0.1, dominance_shift=0.0,
            discrete_emotions={"joy": 0.7},
        )
        state = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        config = RelationshipTransitionConfig.defaults()
        new_state = transition_relationship(state, ap, FIXED_CLOCK + 1, config)
        assert new_state.trust > 0.5
        assert new_state.timestamp > FIXED_CLOCK


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 5: Fail-closed through process_turn
# ═══════════════════════════════════════════════════════════════════════════════

class TestFailClosedThroughProcessTurn:
    """Corrupt snapshots block the entire flow before any external effect."""

    @pytest.mark.parametrize("description,bad_state", [
        ("unknown_version", {"schema_version": 99}),
        ("incomplete_snapshot", {"pleasure": 0.5}),
        ("extra_field", {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0,
                          "libido": 0.0, "aggression": 0.0, "connection": 0.5,
                          "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
                          "last_update": FIXED_CLOCK, "extra_field": "x"}),
        ("invalid_numeric_type", {"pleasure": "not_a_number", "arousal": 0.0,
                                   "dominance": 0.0, "libido": 0.0,
                                   "aggression": 0.0, "connection": 0.5,
                                   "energy": 0.8, "tension": 0.0,
                                   "coping_mode": "HEALTHY",
                                   "last_update": FIXED_CLOCK}),
    ])
    def test_corrupt_snapshot_blocks_flow(self, description, bad_state):
        async def run():
            engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
            engine.memory_manager.load_user_state = MagicMock(return_value={
                "emotional_state": bad_state,
            })

            # Install spies on all downstream methods
            engine.memory_manager.get_context = MagicMock()
            engine.groq_manager.chat_completion_async = MagicMock()
            engine.memory_manager.save_turn = MagicMock()
            engine.memory_manager.sync_state = MagicMock()

            bg_tasks = MagicMock()

            with pytest.raises(EmotionalDomainError):
                await engine.process_turn("user", "Msg", background_tasks=bg_tasks)

            # Zero downstream calls: no context, no async LLM, no persist
            engine.memory_manager.get_context.assert_not_called()
            engine.groq_manager.chat_completion_async.assert_not_called()
            engine.memory_manager.save_turn.assert_not_called()
            engine.memory_manager.sync_state.assert_not_called()
            # transition_relationship is called inside process_turn, but
            # since the corrupt snapshot blocks the flow before transition,
            # sync_state is never called (relationship never reached)
            engine.memory_manager.sync_state.assert_not_called()
            bg_tasks.add_task.assert_not_called()

        asyncio.run(run())

    def test_nan_snapshot_fails_closed(self):
        async def run():
            engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
            engine.memory_manager.load_user_state = MagicMock(return_value={
                "emotional_state": {
                    "pleasure": float("nan"), "arousal": 0.0, "dominance": 0.0,
                    "libido": 0.0, "aggression": 0.0, "connection": 0.5,
                    "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
                    "last_update": FIXED_CLOCK,
                },
            })
            engine.groq_manager.chat_completion_async = MagicMock()
            engine.memory_manager.save_turn = MagicMock()
            engine.memory_manager.sync_state = MagicMock()
            bg_tasks = MagicMock()

            with pytest.raises(EmotionalDomainError):
                await engine.process_turn("user", "Msg", background_tasks=bg_tasks)

            engine.groq_manager.chat_completion_async.assert_not_called()
            engine.memory_manager.save_turn.assert_not_called()
            engine.memory_manager.sync_state.assert_not_called()
            bg_tasks.add_task.assert_not_called()

        asyncio.run(run())

    def test_inf_snapshot_fails_closed(self):
        async def run():
            engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
            engine.memory_manager.load_user_state = MagicMock(return_value={
                "emotional_state": {
                    "pleasure": float("inf"), "arousal": 0.0, "dominance": 0.0,
                    "libido": 0.0, "aggression": 0.0, "connection": 0.5,
                    "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
                    "last_update": FIXED_CLOCK,
                },
            })
            engine.groq_manager.chat_completion_async = MagicMock()
            engine.memory_manager.save_turn = MagicMock()
            engine.memory_manager.sync_state = MagicMock()
            bg_tasks = MagicMock()

            with pytest.raises(EmotionalDomainError):
                await engine.process_turn("user", "Msg", background_tasks=bg_tasks)

            engine.groq_manager.chat_completion_async.assert_not_called()
            engine.memory_manager.save_turn.assert_not_called()
            engine.memory_manager.sync_state.assert_not_called()
            bg_tasks.add_task.assert_not_called()

        asyncio.run(run())

    def test_corrupt_snapshot_blocks_before_transition(self):
        """Also verify transition is never called for corrupt snapshots."""
        async def run():
            engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
            engine.memory_manager.load_user_state = MagicMock(return_value={
                "emotional_state": {"schema_version": 99},
            })
            engine.groq_manager.chat_completion_async = MagicMock()
            engine.memory_manager.save_turn = MagicMock()
            engine.memory_manager.sync_state = MagicMock()

            parse_spy = MagicMock(wraps=parse_llm_appraisal)
            transition_spy = MagicMock(wraps=transition)
            with patch("backend.engine.parse_llm_appraisal", parse_spy), \
                 patch("backend.engine.transition", transition_spy):
                with pytest.raises(EmotionalDomainError):
                    await engine.process_turn("user", "Msg")

            # Parse and transition should never be called
            parse_spy.assert_not_called()
            transition_spy.assert_not_called()

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 6: JSONB persistence payload inspection
# ═══════════════════════════════════════════════════════════════════════════════

class TestJSONBPersistence:
    """sync_state sends the exact v1 dict to Supabase via JSONB."""

    def test_persisted_payload_is_v1_dict(self):
        mm = MemoryManager(clock=lambda: FIXED_CLOCK)
        mm.supabase = MagicMock()
        mock_resp = MagicMock()
        mock_resp.data = [{"user_id": "u"}]
        mock_resp.error = None
        mm.supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_resp

        state = EmotionalStateV1.create(
            pleasure=0.5, arousal=-0.2, dominance=0.3,
            libido=0.1, aggression=0.0, connection=0.7,
            energy=0.9, tension=0.2, coping_mode="HEALTHY",
            timestamp=FIXED_CLOCK,
        )
        rel = RelationshipStateV1.neutral(timestamp=FIXED_CLOCK)
        mm.sync_state("u", state, rel)

        # Capture what was passed to supabase.update()
        call_args = mm.supabase.table.return_value.update.call_args
        assert call_args is not None
        update_data = call_args[0][0]

        # The emotional_state field should be the exact v1 dict
        payload = update_data["emotional_state"]
        assert isinstance(payload, dict)
        assert payload["schema_version"] == 1
        assert payload["timestamp"] == FIXED_CLOCK
        assert payload["pleasure"] == 0.5
        assert payload["coping_mode"] == "HEALTHY"

        # Must NOT contain legacy fields or other layer fields
        assert "last_update" not in payload
        assert "appraisal" not in payload
        assert "regulation" not in payload
        assert "prompt" not in payload

        # Also verify the update filter uses user_id
        eq_call = mm.supabase.table.return_value.update.return_value.eq.call_args
        assert eq_call is not None
        assert eq_call[0] == ("user_id", "u")

        # Relationship payload must also be v1 and not contain user_id or bond_label
        rel_payload = update_data["relationship_state"]
        assert isinstance(rel_payload, dict)
        assert rel_payload["schema_version"] == 1
        assert "user_id" not in rel_payload
        assert "bond_label" not in rel_payload
        assert "last_interaction" not in rel_payload

    def test_persisted_payload_has_only_v1_fields(self):
        """The payload must contain only the fields from EmotionalStateV1.to_dict()."""
        v1_fields = {
            "schema_version", "pleasure", "arousal", "dominance",
            "libido", "aggression", "connection", "energy", "tension",
            "coping_mode", "timestamp",
        }
        state = EmotionalStateV1.neutral(timestamp=FIXED_CLOCK)
        payload = state.to_dict()
        assert set(payload.keys()) == v1_fields


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 7: Archival scheduling test
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Correction 4a: Single relationship transition per turn
# ═══════════════════════════════════════════════════════════════════════════════

class TestSingleRelationshipTransition:
    """Proves exactly one transition_relationship call per successful turn
    and that both transition() and transition_relationship() receive the
    SAME AppraisalV1 object (by identity, not equality).
    """

    def test_one_transition_relationship_per_turn(self):
        async def run():
            engine = _make_engine()
            from backend.emotional_domain import transition as real_emotion_transition
            from backend.emotional_domain import AppraisalV1
            from backend.relationship import (
                transition_relationship as real_rel_transition,
                RelationshipStateV1,
                RelationshipTransitionConfig,
            )

            # Tracking spies that capture the appraisal objects
            emotional_appraisals = []
            def emotion_tracking_spy(*args, **kwargs):
                emotional_appraisals.append(kwargs.get("appraisal", args[1] if len(args) > 1 else None))
                return real_emotion_transition(*args, **kwargs)

            rel_captured_results = []
            rel_appraisals = []
            def rel_tracking_spy(*args, **kwargs):
                rel_appraisals.append(kwargs.get("appraisal", args[1] if len(args) > 1 else None))
                result = real_rel_transition(*args, **kwargs)
                rel_captured_results.append(result)
                return result

            with patch("backend.engine.transition", side_effect=emotion_tracking_spy) as mock_emotion, \
                 patch("backend.engine.transition_relationship", side_effect=rel_tracking_spy) as mock_rel:

                resp, emotions = await engine.process_turn("user", "Hello")

                # ── Exactly one call each ────────────────────────────────────
                assert mock_emotion.call_count == 1
                assert mock_rel.call_count == 1

                # ── Both receive the same AppraisalV1 object (identity, not value) ──
                emotional_appraisal = emotional_appraisals[0]
                rel_appraisal = rel_appraisals[0]
                assert emotional_appraisal is rel_appraisal, (
                    "transition() and transition_relationship() must receive "
                    "the SAME AppraisalV1 object, not two equal copies."
                )

                # ── AppraisalV1 is not a dict ────────────────────────────────
                assert isinstance(emotional_appraisal, AppraisalV1)
                assert not isinstance(emotional_appraisal, dict)

                # ── Inspect relationship transition arguments ────────────────
                rel_kwargs = mock_rel.call_args[1]
                rel_previous_state = rel_kwargs.get("previous_state")
                rel_current_time = rel_kwargs.get("current_time")
                rel_config = rel_kwargs.get("config")

                assert isinstance(rel_previous_state, RelationshipStateV1)
                assert isinstance(rel_config, RelationshipTransitionConfig)

                # current_time matches injected clock
                assert rel_current_time == FIXED_CLOCK
                # Also verify emotional transition receives the same clock
                emotion_kwargs = mock_emotion.call_args[1]
                assert emotion_kwargs.get("current_time") == FIXED_CLOCK

                # ── The result is delivered to sync_state ────────────────────
                assert len(rel_captured_results) == 1
                args_sync, _ = engine.memory_manager.sync_state.call_args
                sync_rel = args_sync[2]
                assert sync_rel is rel_captured_results[0]

        asyncio.run(run())


class TestArchivalScheduling:
    """Archival extraction scheduling: order on success, zero on sync_state failure."""

    def test_success_orders_save_sync_then_schedule(self):
        """On success: save_turn → sync_state → schedule → return."""
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)
            call_order = []
            bg_tasks = MagicMock()

            def mock_save(user_id, user_msg, bot_msg):
                call_order.append("save")
                from backend.archival_memory import PersistedTurnRef
                return PersistedTurnRef(user_id=user_id, source_chat_log_id=1,
                                        assistant_chat_log_id=2)

            def mock_sync(user_id, state, rel):
                call_order.append("sync")

            def mock_schedule(coro, *args, **kwargs):
                call_order.append("schedule")

            engine.memory_manager.save_turn = MagicMock(side_effect=mock_save)
            engine.memory_manager.sync_state = MagicMock(side_effect=mock_sync)
            bg_tasks.add_task = MagicMock(side_effect=mock_schedule)

            resp, emotions = await engine.process_turn("user", "Hello", background_tasks=bg_tasks)

            assert call_order == ["save", "sync", "schedule"]
            bg_tasks.add_task.assert_called_once()
            assert resp is not None

        asyncio.run(run())

    def test_sync_state_failure_blocks_add_task(self):
        """
        When sync_state fails:
        * save pode ter ocorrido;
        * sync é registrado (a falha ocorre depois);
        * schedule não pode aparecer;
        * add_task permanece sem chamadas;
        * nenhuma resposta de sucesso é retornada.
        """
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)
            call_order = []
            bg_tasks = MagicMock()

            def mock_save(user_id, user_msg, bot_msg):
                call_order.append("save")
                from backend.archival_memory import PersistedTurnRef
                return PersistedTurnRef(user_id=user_id, source_chat_log_id=1,
                                        assistant_chat_log_id=2)

            def mock_schedule(coro, *args, **kwargs):
                call_order.append("schedule")

            engine.memory_manager.save_turn = MagicMock(side_effect=mock_save)
            engine.memory_manager.sync_state = MagicMock(
                side_effect=StatePersistenceError("DB fail")
            )
            bg_tasks.add_task = MagicMock(side_effect=mock_schedule)

            with pytest.raises(TurnExecutionError):
                await engine.process_turn("user", "Hello", background_tasks=bg_tasks)

            # schedule must NOT be in call_order; add_task must not be called
            assert "schedule" not in call_order
            bg_tasks.add_task.assert_not_called()

        asyncio.run(run())

    def test_no_background_tasks_parameter_still_works(self):
        """Without background_tasks param, process_turn still succeeds."""
        async def run():
            engine = _make_engine()
            resp, emotions = await engine.process_turn("user", "Hello")
            assert resp is not None
            assert isinstance(emotions, EmotionStateResponse)
            assert emotions.schema_version == 1

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 8: Sanitised logging with unique markers
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Correction 10: First-turn regression test  (clock alignment)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewProfileFirstTurn:
    """
    First turn of a brand-new user with an advancing clock must succeed.

    Before the fix, the new-profile snapshots were created with a *different*
    (later) clock call than ``current_time``, causing ``transition_relationship()``
    to reject the turn with a clock-regression error.

    After the fix, ``load_user_state()`` accepts an explicit ``default_timestamp``
    that matches the turn's ``current_time``, so both emotional and relationship
    snapshots are born with the exact same timestamp as the turn.
    """

    def test_first_turn_with_advancing_clock(self):
        async def run():
            # ── Advancing clock that would have masked the defect          ──
            times = iter([100.0, 100.1, 100.2, 100.3])
            clock = lambda: next(times)

            # ── Isolate from real SentenceTransformer (would hang in CI)  ──
            with patch("backend.memory.SentenceTransformer", return_value=MagicMock()) as embedding_cls:
                engine = ConversationEngine(clock=clock)
            embedding_cls.assert_called_once()

            # ── Mock Supabase: empty select (new user), then insert OK     ──
            engine.memory_manager.supabase = MagicMock()
            mock_select = MagicMock(data=[], error=None)
            engine.memory_manager.supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_select  # noqa: E501
            mock_insert = MagicMock(data=[{"user_id": "new_user"}], error=None)
            engine.memory_manager.supabase.table.return_value.insert.return_value.execute.return_value = mock_insert  # noqa: E501

            # ── Mock other engine dependencies                             ──
            engine.memory_manager.get_context = MagicMock(return_value="[ctx]")
            engine.memory_manager.get_context_components = MagicMock(return_value={
                "persona": "Katherine...",
                "user_profile_str": "{}",
                "memory_str": "",
                "history_list": [],
                "assembled": "[ctx]",
            })
            from backend.trusted_context import LoadedContextData
            engine.memory_manager.load_context_data = MagicMock(return_value=LoadedContextData(
                history_rows=(),
                retrieved_memories=(),
                profile_snapshot={},
                persona_snapshot="",
            ))
            engine.memory_manager.sync_state = MagicMock()
            engine.memory_manager.save_turn = MagicMock()
            
            async def _mock_appraisal_new(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = json.dumps({
                    "valence": 0.2, "arousal_shift": 0.1, "dominance_shift": 0.0,
                    "triggered_emotions": {"joy": 0.3},
                })
                return mock_resp
            
            async def _mock_generation_new(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = "Hello!"
                return mock_resp
            
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_appraisal_new)

            # ── Imports for real transition functions / spies             ──
            from backend.emotional_domain import transition as real_e_transition
            from backend.emotional_domain import AppraisalV1
            from backend.relationship import (
                transition_relationship as real_r_transition,
                RelationshipStateV1,
                RelationshipTransitionConfig,
            )

            emotional_appraisals: list = []
            rel_appraisals: list = []
            rel_results: list = []

            def emotion_spy(*args, **kwargs):
                ap = kwargs.get("appraisal", args[1] if len(args) > 1 else None)
                emotional_appraisals.append(ap)
                return real_e_transition(*args, **kwargs)

            def rel_spy(*args, **kwargs):
                ap = kwargs.get("appraisal", args[1] if len(args) > 1 else None)
                rel_appraisals.append(ap)
                result = real_r_transition(*args, **kwargs)
                rel_results.append(result)
                return result

            with patch("backend.engine.transition", side_effect=emotion_spy) as m_e, \
                 patch("backend.engine.transition_relationship", side_effect=rel_spy) as m_r:

                resp, emotions = await engine.process_turn("new_user", "Hello")

                # ── 1. First turn succeeds ────────────────────────────────
                assert resp is not None

                # ── 2. Profile created for authenticated user_id ──────────
                insert_call = engine.memory_manager.supabase.table.return_value.insert.call_args
                assert insert_call is not None
                inserted = insert_call[0][0]
                assert inserted["user_id"] == "new_user"

                # ── 3 & 4 & 5. Both snapshots share turn timestamp ────────
                assert inserted["emotional_state"]["timestamp"] == 100.0
                assert inserted["relationship_state"]["timestamp"] == 100.0

                # ── 6 & 7 & 8 & 9. Relationship payload invariants ────────
                rel_payload = inserted["relationship_state"]
                assert rel_payload["schema_version"] == 1
                assert "user_id" not in rel_payload
                assert "bond_label" not in rel_payload
                assert "last_interaction" not in rel_payload

                # ── 10 & 11. Exactly one transition call each ─────────────
                assert m_e.call_count == 1
                assert m_r.call_count == 1

                # ── 12 & 13. Same AppraisalV1 object by identity ──────────
                emotional_appraisal = emotional_appraisals[0]
                rel_appraisal = rel_appraisals[0]
                assert emotional_appraisal is rel_appraisal
                assert isinstance(emotional_appraisal, AppraisalV1)
                assert not isinstance(emotional_appraisal, dict)

                # ── 14. sync_state receives the transition result ─────────
                assert len(rel_results) == 1
                args_sync, _ = engine.memory_manager.sync_state.call_args
                assert args_sync[2] is rel_results[0]

                # ── Current time is 100.0 (first clock call) ──────────────
                assert m_r.call_args[1]["current_time"] == 100.0
                assert m_e.call_args[1]["current_time"] == 100.0

                # ── Config type check ─────────────────────────────────────
                assert isinstance(m_r.call_args[1]["config"], RelationshipTransitionConfig)
                assert isinstance(m_r.call_args[1]["previous_state"], RelationshipStateV1)

        asyncio.run(run())


class TestSanitisedLogging:
    """Fallback logs contain only event and code, never sensitive markers."""

    SENSITIVE_PAYLOAD = "SENSITIVE_USER_PAYLOAD_92841"
    SENSITIVE_KEY = "SENSITIVE_EXTRA_KEY_92841"
    SENSITIVE_PROMPT = "SENSITIVE_PROMPT_92841"
    SENSITIVE_EXCEPTION = "SENSITIVE_EXCEPTION_92841"

    def test_fallback_logs_only_sanitised_code(self):
        async def run():
            engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
            engine.memory_manager.load_user_state = MagicMock(return_value={
                "emotional_state": _legacy_emotion_dict(),
            })
            engine.memory_manager.sync_state = MagicMock()
            engine.memory_manager.save_turn = MagicMock()
            engine.memory_manager.get_context = MagicMock(return_value="[mocked context]")
            engine.memory_manager.get_context_components = MagicMock(return_value={
                "persona": "Katherine...",
                "user_profile_str": "{}",
                "memory_str": "",
                "memory_entries": [],
                "history_list": [],
                "assembled": "[mocked context]",
            })
            from backend.trusted_context import LoadedContextData
            engine.memory_manager.load_context_data = MagicMock(return_value=LoadedContextData(
                history_rows=(),
                retrieved_memories=(),
                profile_snapshot={},
                persona_snapshot="",
            ))
            async def _mock_bad_appraisal(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = json.dumps({
                    "valence": self.SENSITIVE_PAYLOAD,
                    "arousal_shift": 0.0,
                    "dominance_shift": 0.0,
                    self.SENSITIVE_KEY: "should_not_leak",
                    "triggered_emotions": {"joy": 0.5},
                })
                return mock_resp
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_bad_appraisal)

            logger = logging.getLogger("backend.engine")
            logger.setLevel(logging.INFO)
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger.addHandler(handler)
            try:
                with pytest.raises(TurnExecutionError):
                    await engine.process_turn("user", self.SENSITIVE_PAYLOAD)
            finally:
                logger.removeHandler(handler)

            log_text = stream.getvalue()

            # Must contain the sanitised event and code
            assert "event=emotional_appraisal_fallback" in log_text
            assert "code=" in log_text

            # Must NOT contain any sensitive marker
            assert self.SENSITIVE_PAYLOAD not in log_text
            assert self.SENSITIVE_KEY not in log_text
            assert self.SENSITIVE_PROMPT not in log_text
            assert "SENSITIVE_" not in log_text

        asyncio.run(run())

    def test_exception_marker_not_leaked_via_caplog(self, caplog):
        """
        Uma exceção contendo ``SENSITIVE_EXCEPTION_92841`` é injetada na
        fronteira do parser. O ``except Exception`` interno de
        ``parse_llm_appraisal`` captura a falha sem vazar o texto
        da exceção para os logs.
        """
        async def run():
            engine = _make_engine()
            engine.memory_manager.sync_state = MagicMock()
            engine.memory_manager.save_turn = MagicMock()

            with patch(
                "backend.emotional_domain.appraisal_parser._require_finite_float_in_range",
                side_effect=Exception(self.SENSITIVE_EXCEPTION),
            ):
                with pytest.raises(TurnExecutionError):
                    await engine.process_turn("user", "Hello")

        with caplog.at_level(logging.INFO):
            asyncio.run(run())

        caplog_text = caplog.text
        assert "event=emotional_appraisal_fallback" in caplog_text
        assert "unexpected_parser_failure" in caplog_text
        assert self.SENSITIVE_EXCEPTION not in caplog_text
        assert "SENSITIVE_" not in caplog_text

    def test_empty_appraisal_content_fails_with_invalid_response(self):
        """None content from appraisal is treated as provider_invalid_response."""
        async def run():
            engine = _make_engine()
            async def _mock_none(**kwargs):
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = None
                return mock_resp
            engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_none)

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# Correction 9: Exact public contract test (updated for EmotionStateResponse)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPublicContract:
    """Public response is an EmotionStateResponse with versioned v1 fields."""

    EXPECTED_PUBLIC_FIELDS = {
        "schema_version",
        "mood_label",
        "pad",
        "dominant_emotions",
        "timestamp",
    }

    def test_projection_is_emotion_state_response(self):
        state = EmotionalStateV1.create(
            pleasure=0.5, arousal=-0.2, dominance=0.3,
            libido=0.1, aggression=0.0, connection=0.7,
            energy=0.9, tension=0.2, coping_mode="HEALTHY",
            timestamp=FIXED_CLOCK,
        )
        appraisal = AppraisalV1.create(
            valence_shift=0.2, arousal_shift=0.1, dominance_shift=0.0,
            discrete_emotions={"joy": 0.5},
        )
        projected = ConversationEngine._project_emotion_state(state, appraisal)
        assert isinstance(projected, EmotionStateResponse)
        data = projected.model_dump()
        assert set(data.keys()) == self.EXPECTED_PUBLIC_FIELDS

    def test_serialized_format_does_not_contain_internal_fields(self):
        state = EmotionalStateV1.create(
            pleasure=0.0, arousal=0.0, dominance=0.0,
            libido=0.0, aggression=0.0, connection=0.5,
            energy=0.8, tension=0.0, coping_mode="HEALTHY",
            timestamp=FIXED_CLOCK,
        )
        appraisal = AppraisalV1.neutral()
        projected = ConversationEngine._project_emotion_state(state, appraisal)
        json_str = projected.model_dump_json()

        # Must contain the new contract fields
        assert '"schema_version"' in json_str
        assert '"mood_label"' in json_str
        assert '"timestamp"' in json_str

        # Must NOT contain internal fields
        forbidden = [
            "acting_instruction", "coping_mode", "libido", "aggression",
            "connection", "energy", "tension", "regulation", "fallback",
            "last_update",
        ]
        for field in forbidden:
            assert field not in json_str, f"Forbidden field '{field}' found in JSON"

    def test_timestamp_preserved(self):
        state = EmotionalStateV1.neutral(timestamp=FIXED_CLOCK)
        appraisal = AppraisalV1.neutral()
        projected = ConversationEngine._project_emotion_state(state, appraisal)
        assert projected.timestamp == FIXED_CLOCK
        assert projected.timestamp == state.timestamp

    def test_process_turn_returns_emotion_state_response(self):
        async def run():
            engine = _make_engine()
            resp, emotions = await engine.process_turn("user", "Hello")
            assert isinstance(emotions, EmotionStateResponse)
            assert emotions.schema_version == 1
            assert emotions.mood_label is not None
            assert emotions.timestamp == FIXED_CLOCK

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy flow verification & existing behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestLegacyFlowNotUsed:
    """The old AffectiveEngine/OCC/Normalize are not in the active path."""

    def test_legacy_flow_classes_not_called(self):
        from backend.engine import ConversationEngine
        engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
        assert not hasattr(engine, "_normalize_perception")
        assert hasattr(engine, "presentation")
        assert not hasattr(engine, "affective_engine") or "affective_engine" not in dir(engine)

    def test_snapshot_migration_preserves_values(self):
        legacy = _legacy_emotion_dict(pleasure=0.7)
        v1 = migrate_legacy_snapshot(legacy)
        assert isinstance(v1, EmotionalStateV1)
        assert v1.pleasure == 0.7
        assert v1.timestamp == legacy["last_update"]

    def test_migrated_snapshot_persisted_as_v1(self):
        legacy = _legacy_emotion_dict(pleasure=0.7)
        v1 = migrate_legacy_snapshot(legacy)
        persisted = v1.to_dict()
        assert "schema_version" in persisted
        assert "timestamp" in persisted
        assert "last_update" not in persisted

    def test_v1_snapshot_loaded_cleanly(self):
        v1_dict = _v1_emotion_dict(pleasure=0.3)
        result = migrate_legacy_snapshot(v1_dict)
        assert isinstance(result, EmotionalStateV1)
        assert result.pleasure == 0.3

    def test_unknown_version_fails_closed(self):
        with pytest.raises(EmotionalDomainError):
            migrate_legacy_snapshot({"schema_version": 99})

    def test_incomplete_snapshot_fails_closed(self):
        with pytest.raises(EmotionalDomainError):
            migrate_legacy_snapshot({"pleasure": 0.5})

    def test_load_failure_blocks_processing(self):
        async def run():
            engine = ConversationEngine(clock=lambda: FIXED_CLOCK)
            engine.memory_manager.supabase = MagicMock()
            engine.memory_manager.supabase.table.return_value.select.return_value.eq.return_value.execute.side_effect = Exception("DB down")
            engine.groq_manager.chat_completion_async = MagicMock()

            with pytest.raises(TurnExecutionError):
                await engine.process_turn("user", "Msg")

        asyncio.run(run())

    def test_new_profile_uses_v1_state(self):
        mm = MemoryManager(clock=lambda: FIXED_CLOCK)
        default = mm._get_default_state("new_user")
        emotional_state = default["emotional_state"]
        assert isinstance(emotional_state, dict)
        assert "schema_version" in emotional_state
        assert emotional_state["schema_version"] == 1
        assert "timestamp" in emotional_state
        assert "last_update" not in emotional_state

    def test_sync_state_failure_propagates(self):
        async def run():
            engine = _make_engine()
            engine.memory_manager.sync_state = MagicMock(side_effect=StatePersistenceError())

            with pytest.raises(TurnExecutionError):
                await engine.process_turn("user", "Msg")

        asyncio.run(run())
