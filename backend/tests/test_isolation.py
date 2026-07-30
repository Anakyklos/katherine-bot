import asyncio
import pytest
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch
from backend.engine import ConversationEngine
from backend.emotional_core import EmotionalState, AffectiveEngine
from backend.emotional_domain import AppraisalV1, EmotionalStateV1, ParseErrorCode, parse_llm_appraisal
from backend.relationship import RelationshipStateV1
from backend.memory import StatePersistenceError, StateLoadError, MemoryManager
from backend.turn_execution import TurnExecutionError, TurnErrorCode


# ── Helper: minimal valid legacy emotional state dict ────────────────────────

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
        "last_update": time.time(),
    }


@pytest.fixture(autouse=True)
def _mock_sentence_transformer():
    """Prevent real SentenceTransformer model loading."""
    with patch("backend.memory.SentenceTransformer", return_value=MagicMock()):
        yield


@pytest.fixture(autouse=True)
def mock_load_recent_history(monkeypatch):
    monkeypatch.setattr(MemoryManager, "load_recent_history", lambda self, user_id, limit=10: [])


def _make_mock_appraisal():
    """Return AppraisalV1.neutral — a valid appraisal that passes _appraise validation."""
    return AppraisalV1.neutral()


def _make_mock_generate_response(text="Hi"):
    """Return a plain text response that _generate can return."""
    return text


def _add_trusted_context_mocks(engine):
    """Add mocks for the trusted context flow to the engine for testing.

    Must be called after engine.memory_manager.save_turn and sync_state are set.
    """
    from backend.trusted_context import ContextBundle
    engine.memory_manager.build_context_bundle = MagicMock(return_value=ContextBundle(
        trusted_policy="You are a test assistant.",
    ))
    engine._generate_with_messages = AsyncMock(return_value="Hi")


def test_deterministic_transition():
    engine = AffectiveEngine()
    state = EmotionalState(pleasure=0.1, arousal=0.2, dominance=0.3)
    current_time = 1000.0
    user_input = "Hello"
    res1, inst1 = engine.update_state(state, user_input, current_time)
    res2, inst2 = engine.update_state(state, user_input, current_time)
    assert res1 == res2
    assert inst1 == inst2


def test_no_mutation():
    engine = AffectiveEngine()
    state = EmotionalState(pleasure=0.1, arousal=0.2, dominance=0.3)
    initial_dict = state.to_dict()
    current_time = 1000.0
    new_state, _ = engine.update_state(state, "Hello", current_time)
    assert state.to_dict() == initial_dict
    assert new_state != state


def test_user_isolation():
    async def run_test():
        engine = ConversationEngine()
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.sync_state = MagicMock()
        engine.memory_manager.save_turn = MagicMock()
        states = {
            "A": {"emotional_state": _legacy_emotion_dict(pleasure=0.5)},
            "B": {"emotional_state": _legacy_emotion_dict(pleasure=-0.5)}
        }
        engine.memory_manager.load_user_state = MagicMock(side_effect=lambda uid, **kwargs: states.get(uid, {}))
        engine._perceive = MagicMock(return_value={})
        _add_trusted_context_mocks(engine)
        _, state_a = await engine.process_turn("A", "Msg A")
        _, state_b = await engine.process_turn("B", "Msg B")
        assert state_a.pad.pleasure > 0
        assert state_b.pad.pleasure < 0
    asyncio.run(run_test())


def test_identity_binding():
    async def run_test():
        engine = ConversationEngine()
        auth_id = "auth_user"
        engine.memory_manager.load_user_state = MagicMock(return_value={
            "relationship_state": {"user_id": "wrong", "trust": 0.5, "affection": 0.3,
                                    "tension": 0.0, "triggers": [], "last_interaction": time.time()},
            "emotional_state": _legacy_emotion_dict(),
        })
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.save_turn = MagicMock()
        sync_mock = MagicMock()
        engine.memory_manager.sync_state = sync_mock
        engine._perceive = MagicMock(return_value={})
        _add_trusted_context_mocks(engine)
        await engine.process_turn(auth_id, "Hello")
        args, _ = sync_mock.call_args
        assert args[0] == auth_id
        # The relationship is now RelationshipStateV1 (no user_id field)
        # Identity is verified by the user_id argument, not from the state
        assert isinstance(args[2], RelationshipStateV1)
    asyncio.run(run_test())


def test_fail_closed_load():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.supabase = MagicMock()
        engine.memory_manager.supabase.table.return_value.select.return_value.eq.return_value.execute.side_effect = Exception("RAW")
        engine._perceive = MagicMock(return_value={})
        # _appraise and _generate won't be reached because load_state will fail.
        # run_blocking_read wraps the raw exception as TurnExecutionError.
        with pytest.raises(TurnExecutionError) as exc:
            await engine.process_turn("user", "Msg")
        assert exc.value.code == TurnErrorCode.persistence_unavailable
    asyncio.run(run_test())


def test_persistence_failure_zero_rows():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.load_user_state = MagicMock(return_value={"emotional_state": _legacy_emotion_dict()})
        engine.memory_manager.supabase = MagicMock()
        engine.memory_manager.supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={})
        _add_trusted_context_mocks(engine)
        with pytest.raises(TurnExecutionError) as exc:
            await engine.process_turn("user", "Msg")
        assert exc.value.code == TurnErrorCode.persistence_unavailable
    asyncio.run(run_test())


def test_appraisal_fallback_sanitized():
    """parse_llm_appraisal on invalid input returns neutral fallback with sanitized code."""
    result = parse_llm_appraisal(None)
    assert result.is_fallback
    assert result.error_code == ParseErrorCode.invalid_structure
    assert result.appraisal == AppraisalV1.neutral()


def test_concurrent_requests_serialization():
    async def run_test():
        engine = ConversationEngine()
        user_id = "test_user"
        db = {user_id: {"emotional_state": _legacy_emotion_dict(pleasure=0.0)}}
        engine.memory_manager.load_user_state = MagicMock(side_effect=lambda uid, **kwargs: db[uid].copy())
        def mock_sync(uid, state, rel, profile=None): db[uid]["emotional_state"] = state.to_dict()
        engine.memory_manager.sync_state = MagicMock(side_effect=mock_sync)
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={"valence": 0.3, "arousal_shift": 0.0, "dominance_shift": 0.0})

        # _appraise is called first (generates appraisal), then _generate (generates response).
        # For this test we need _appraise to set a signal that t1 has entered the provider call.
        # The existing _perceive mock is no longer used by the new engine; _appraise replaces it.
        req1_in = asyncio.Event()
        async def async_appraise_mock(*args, **kwargs):
            req1_in.set()
            await asyncio.sleep(0.2)
            # Return a positive appraisal so the emotional state pleasure > 0 after transition
            return AppraisalV1.create(
                valence_shift=0.3, arousal_shift=0.0, dominance_shift=0.0,
                discrete_emotions={}, schema_version=1,
            )
        engine._appraise = AsyncMock(side_effect=async_appraise_mock)
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        _add_trusted_context_mocks(engine)

        t1 = asyncio.create_task(engine.process_turn(user_id, "T1"))
        await req1_in.wait()
        t2 = asyncio.create_task(engine.process_turn(user_id, "T2"))
        await asyncio.gather(t1, t2)
        assert db[user_id]["emotional_state"]["pleasure"] > 0.15
    asyncio.run(run_test())


def test_no_global_lock():
    async def run_test():
        engine = ConversationEngine()
        barrier = threading.Barrier(2)
        async def async_appraise_mock(*args, **kwargs):
            await asyncio.to_thread(barrier.wait, timeout=2)
            return _make_mock_appraisal()
        engine._appraise = AsyncMock(side_effect=async_appraise_mock)
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.load_user_state = MagicMock(return_value={"emotional_state": _legacy_emotion_dict()})
        engine.memory_manager.sync_state = MagicMock()
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={})
        _add_trusted_context_mocks(engine)
        await asyncio.gather(engine.process_turn("A", "M"), engine.process_turn("B", "M"))
    asyncio.run(run_test())


def test_lock_cleanup():
    async def run_test():
        engine = ConversationEngine()
        user_id = "cleanup_user"
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.load_user_state = MagicMock(return_value={"emotional_state": _legacy_emotion_dict()})
        engine.memory_manager.sync_state = MagicMock()
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={})
        _add_trusted_context_mocks(engine)

        await engine.process_turn(user_id, "Msg")
        async with engine.lock_manager._dict_lock:
            assert user_id not in engine.lock_manager._locks

        engine.memory_manager.sync_state.side_effect = Exception("Fail")
        try: await engine.process_turn(user_id, "Msg")
        except Exception: pass
        async with engine.lock_manager._dict_lock:
            assert user_id not in engine.lock_manager._locks
    asyncio.run(run_test())


def test_lock_cleanup_on_cancellation_during_thread_work():
    async def run_test():
        engine = ConversationEngine()
        user_id = "cancel_thread_user"

        import threading
        load_reached = threading.Event()
        load_release = threading.Event()
        load_finished = False

        def mock_load(uid, **kwargs):
            load_reached.set()
            load_release.wait(timeout=2)
            nonlocal load_finished
            load_finished = True
            return {
                "emotional_state": _legacy_emotion_dict(),
                # Use a fixed low timestamp to avoid clock regression with real time.time() clock
                "relationship_state": RelationshipStateV1.neutral(timestamp=500.0).to_dict()
            }

        engine.memory_manager.load_user_state = MagicMock(side_effect=mock_load)
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.sync_state = MagicMock()
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={"valence": 0.0})
        _add_trusted_context_mocks(engine)

        # 1. Start process_turn
        task1 = asyncio.create_task(engine.process_turn(user_id, "Msg 1"))

        # Wait for task1 to reach mock_load in the worker thread
        for _ in range(40):
            if load_reached.is_set():
                break
            await asyncio.sleep(0.05)
        assert load_reached.is_set()

        # 2. While task1 is blocked, start task2 for the same user
        task2_started = False
        task2_done = False
        async def run_task2():
            nonlocal task2_started, task2_done
            task2_started = True
            await engine.process_turn(user_id, "Msg 2")
            task2_done = True

        task2 = asyncio.create_task(run_task2())
        await asyncio.sleep(0.1) # Let task2 queue up on the lock

        # Cancel task1 while it is blocked inside mock_load (in a thread)
        task1.cancel()
        await asyncio.sleep(0.1)

        # task1 is now cancelled — in asyncio, cancelling a task that is awaiting
        # wait_for(to_thread(...)) makes the task done(cancelled=True) immediately,
        # even though the underlying thread continues running.
        assert task1.cancelled()
        assert not task2_done

        # Release the thread block
        load_release.set()

        # Now task1 should propagate CancelledError
        try:
            await task1
        except asyncio.CancelledError:
            pass

        # task2 can now acquire the lock and finish
        await task2
        assert task2_done
        assert load_finished

        # Lock entry is cleaned up
        async with engine.lock_manager._dict_lock:
            assert user_id not in engine.lock_manager._locks

    asyncio.run(run_test())


def test_lock_cleanup_on_cancellation_during_sync_state():
    async def run_test():
        engine = ConversationEngine()
        user_id = "cancel_sync_user"

        import threading
        sync_reached = threading.Event()
        sync_release = threading.Event()
        sync_finished = False

        def mock_sync(uid, state, rel):
            sync_reached.set()
            sync_release.wait(timeout=2)
            nonlocal sync_finished
            sync_finished = True

        engine.memory_manager.load_user_state = MagicMock(return_value={"emotional_state": _legacy_emotion_dict()})
        engine.memory_manager.get_context = MagicMock(return_value="mock context")
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.sync_state = MagicMock(side_effect=mock_sync)
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={"valence": 0.0})
        _add_trusted_context_mocks(engine)

        # 1. Start process_turn
        task1 = asyncio.create_task(engine.process_turn(user_id, "Msg 1"))

        # Wait for task1 to reach mock_sync in the worker thread
        for _ in range(40):
            if sync_reached.is_set():
                break
            await asyncio.sleep(0.05)
        assert sync_reached.is_set()

        # Cancel task1 while it is blocked inside mock_sync
        task1.cancel()
        await asyncio.sleep(0.1)

        # Release the thread block
        sync_release.set()

        # Now task1 should complete and propagate CancelledError
        try:
            await task1
        except asyncio.CancelledError:
            pass

        assert sync_finished

        # Lock entry is cleaned up
        async with engine.lock_manager._dict_lock:
            assert user_id not in engine.lock_manager._locks

    asyncio.run(run_test())


def test_lock_cleanup_on_cancellation_during_waiting():
    async def run_test():
        engine = ConversationEngine()
        user_id = "waiter_cancel_user"

        import threading
        load_reached = threading.Event()
        load_release = threading.Event()

        def mock_load(uid, **kwargs):
            load_reached.set()
            load_release.wait(timeout=2)
            return {"emotional_state": _legacy_emotion_dict()}

        engine.memory_manager.load_user_state = MagicMock(side_effect=mock_load)
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.sync_state = MagicMock()
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={})
        _add_trusted_context_mocks(engine)

        # Task 1 holds the lock and blocks on load
        task1 = asyncio.create_task(engine.process_turn(user_id, "Msg 1"))

        # Wait for Task 1 to enter load
        for _ in range(40):
            if load_reached.is_set():
                break
            await asyncio.sleep(0.05)
        assert load_reached.is_set()

        # Task 2 queues up on the lock (waiting)
        task2 = asyncio.create_task(engine.process_turn(user_id, "Msg 2"))
        await asyncio.sleep(0.1) # Let task2 wait

        # Check ref count is 2
        async with engine.lock_manager._dict_lock:
            assert engine.lock_manager._locks[user_id][1] == 2

        # Cancel Task 2 (the waiter) while Task 1 is still holding the lock
        task2.cancel()

        # Wait for task2 to propagate CancelledError
        try:
            await task2
        except asyncio.CancelledError:
            pass

        # Ref count should be decremented to 1, but entry must still exist because Task 1 holds it
        async with engine.lock_manager._dict_lock:
            assert user_id in engine.lock_manager._locks
            assert engine.lock_manager._locks[user_id][1] == 1

        # Release Task 1
        load_release.set()
        await task1

        # Lock entry should be completely removed now
        async with engine.lock_manager._dict_lock:
            assert user_id not in engine.lock_manager._locks

    asyncio.run(run_test())


def test_lock_cleanup_on_repeated_cancellation_during_thread_work():
    async def run_test():
        engine = ConversationEngine()
        user_id = "repeated_cancel_user"

        import threading
        load_reached = threading.Event()
        load_release = threading.Event()
        load_finished = False

        def mock_load(uid, **kwargs):
            load_reached.set()
            load_release.wait(timeout=2)
            nonlocal load_finished
            load_finished = True
            return {
                "emotional_state": _legacy_emotion_dict(),
                # Use a fixed low timestamp to avoid clock regression with real time.time() clock
                "relationship_state": RelationshipStateV1.neutral(timestamp=500.0).to_dict()
            }

        engine.memory_manager.load_user_state = MagicMock(side_effect=mock_load)
        engine._appraise = AsyncMock(return_value=_make_mock_appraisal())
        engine._generate = AsyncMock(return_value=_make_mock_generate_response())
        engine.memory_manager.sync_state = MagicMock()
        engine.memory_manager.save_turn = MagicMock()
        engine._perceive = MagicMock(return_value={"valence": 0.0})
        _add_trusted_context_mocks(engine)

        # 1. Start request 1 (task1)
        task1 = asyncio.create_task(engine.process_turn(user_id, "Msg 1"))

        # Wait for task1 to reach mock_load in the worker thread
        for _ in range(40):
            if load_reached.is_set():
                break
            await asyncio.sleep(0.05)
        assert load_reached.is_set()

        # 2. Start request 2 (task2) for the same user
        task2_started = False
        task2_done = False
        async def run_task2():
            nonlocal task2_started, task2_done
            task2_started = True
            await engine.process_turn(user_id, "Msg 2")
            task2_done = True

        task2 = asyncio.create_task(run_task2())
        await asyncio.sleep(0.1) # Let task2 queue up/block on lock

        # 3. Call cancel() twice on task1 while the thread is blocked
        task1.cancel()
        await asyncio.sleep(0.05)
        task1.cancel()
        await asyncio.sleep(0.05)

        # 4. Release the thread block
        load_release.set()

        # Now task1 should complete and propagate CancelledError
        try:
            await task1
        except asyncio.CancelledError:
            pass

        # Task 2 can now acquire the lock and finish
        await task2
        assert task2_done
        assert load_finished

        # Lock entry is cleaned up
        async with engine.lock_manager._dict_lock:
            assert user_id not in engine.lock_manager._locks

    asyncio.run(run_test())
