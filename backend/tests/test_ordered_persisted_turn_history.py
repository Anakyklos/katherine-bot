import pytest
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch, ANY
from backend.memory import MemoryManager, ContextLoadError, TurnPersistenceError
from backend.engine import ConversationEngine


def _valid_legacy_emotion_dict():
    import time
    return {
        "pleasure": 0.0,
        "arousal": 0.0,
        "dominance": 0.0,
        "libido": 0.5,
        "aggression": 0.0,
        "connection": 0.5,
        "energy": 0.8,
        "tension": 0.0,
        "coping_mode": "HEALTHY",
        "last_update": time.time(),
    }


def test_memory_manager_has_no_short_term_memory():
    mm = MemoryManager()
    assert not hasattr(mm, 'short_term_memory')

def test_load_recent_history_validation_failures():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    # Mock None response
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = None
    with pytest.raises(ContextLoadError) as exc:
        mm.load_recent_history("user123")
    assert "user123" not in str(exc.value)

    # Mock error attribute
    mock_resp = MagicMock()
    mock_resp.error = "database error"
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp
    with pytest.raises(ContextLoadError) as exc:
        mm.load_recent_history("user123")
    assert "user123" not in str(exc.value)
    assert "database error" not in str(exc.value)

    # Case 1: Supabase client is falsy / unavailable
    mm.supabase = None
    with pytest.raises(ContextLoadError) as exc:
        mm.load_recent_history("user456")
    assert "user456" not in str(exc.value)

    # Restore mock for subsequent tests
    mm.supabase = MagicMock()

    # Case 2a: Response lacks a `.data` attribute
    mock_resp_no_data_attr = MagicMock()
    if hasattr(mock_resp_no_data_attr, "data"):
        delattr(mock_resp_no_data_attr, "data")
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_no_data_attr
    with pytest.raises(ContextLoadError) as exc:
        mm.load_recent_history("user789")
    assert "user789" not in str(exc.value)

    # Case 2b: Response has `.data` attribute but it is None
    mock_resp_data_none = MagicMock()
    mock_resp_data_none.data = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_data_none
    with pytest.raises(ContextLoadError) as exc:
        mm.load_recent_history("user789")
    assert "user789" not in str(exc.value)

    # Case 3: Generic exception raised in the Supabase call chain
    mm.supabase = MagicMock()
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.side_effect = Exception(
        "unexpected supabase failure for user123"
    )
    with pytest.raises(ContextLoadError) as exc:
        mm.load_recent_history("user123")
    assert "unexpected supabase failure for user123" not in str(exc.value)
    assert "user123" not in str(exc.value)

    # Reset side_effect so that subsequent tests can use return_value
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.side_effect = None

    # Case 4: response.data is not a list
    mock_resp_not_list = MagicMock()
    mock_resp_not_list.data = "not a list"
    mock_resp_not_list.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_not_list
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 5: item is not a dict
    mock_resp_item_not_dict = MagicMock()
    mock_resp_item_not_dict.data = ["not a dict"]
    mock_resp_item_not_dict.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_item_not_dict
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 6: missing role
    mock_resp_missing_role = MagicMock()
    mock_resp_missing_role.data = [{"content": "hello"}]
    mock_resp_missing_role.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_missing_role
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 7: missing content
    mock_resp_missing_content = MagicMock()
    mock_resp_missing_content.data = [{"role": "user"}]
    mock_resp_missing_content.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_missing_content
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 8: unknown role
    mock_resp_unknown_role = MagicMock()
    mock_resp_unknown_role.data = [{"role": "admin", "content": "hello"}]
    mock_resp_unknown_role.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_unknown_role
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 9: content not a string
    mock_resp_content_not_str = MagicMock()
    mock_resp_content_not_str.data = [{"role": "user", "content": 123}]
    mock_resp_content_not_str.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_content_not_str
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 10: content exceeds limit
    mock_resp_exceeds_limit = MagicMock()
    mock_resp_exceeds_limit.data = [{"role": "user", "content": "a" * 10001}]
    mock_resp_exceeds_limit.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_exceeds_limit
    with pytest.raises(ContextLoadError):
        mm.load_recent_history("user123")

    # Case 11: valid payload with extra keys normalized to only role and content
    mock_resp_valid_extra = MagicMock()
    mock_resp_valid_extra.data = [{"role": "user", "content": "hello", "extra_key": "some_value", "id": 1}]
    mock_resp_valid_extra.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = mock_resp_valid_extra
    history = mm.load_recent_history("user123")
    assert history == [{"role": "user", "content": "hello"}]


def test_save_turn_zero_inserted_rows():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock()
    mock_resp.data = [] # zero inserted rows (should be 2)
    mock_resp.error = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "user123" not in str(exc.value)
    assert "hi" not in str(exc.value)
    assert "hello" not in str(exc.value)

def test_save_turn_incomplete_rows():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock()
    mock_resp.data = [{"id": 1}] # only one inserted row (should be 2)
    mock_resp.error = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "user123" not in str(exc.value)
    assert "hi" not in str(exc.value)
    assert "hello" not in str(exc.value)

def test_save_turn_validation_failures():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    # Case 1: Supabase client is falsy / unavailable
    mm.supabase = None
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user456", "hi", "hello")
    assert "user456" not in str(exc.value)
    assert "hi" not in str(exc.value)
    
    # Restore mock
    mm.supabase = MagicMock()
    
    # Case 2: Response is None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = None
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "user123" not in str(exc.value)
    
    # Case 3: Response has error attribute set
    mock_resp_err = MagicMock()
    mock_resp_err.error = "db write error"
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp_err
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "user123" not in str(exc.value)
    assert "db write error" not in str(exc.value)
    
    # Case 4a: Response lacks data attribute
    mock_resp_no_data = MagicMock()
    if hasattr(mock_resp_no_data, "data"):
        delattr(mock_resp_no_data, "data")
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp_no_data
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "user123" not in str(exc.value)
    
    # Case 4b: Response has data but it is None
    mock_resp_data_none = MagicMock()
    mock_resp_data_none.data = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp_data_none
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "user123" not in str(exc.value)
    
    # Case 5: Generic exception in Supabase insert chain
    mm.supabase.table.return_value.insert.return_value.execute.side_effect = Exception("network failure for user123")
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", "hi", "hello")
    assert "network failure for user123" not in str(exc.value)
    assert "user123" not in str(exc.value)

def test_save_turn_exactly_at_limit():
    from backend.memory import MAX_MESSAGE_LENGTH
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    user_msg = "a" * MAX_MESSAGE_LENGTH
    bot_msg = "b" * MAX_MESSAGE_LENGTH
    
    mock_resp = MagicMock()
    mock_resp.data = [
        {"id": 1, "user_id": "user123", "role": "user", "content": user_msg},
        {"id": 2, "user_id": "user123", "role": "assistant", "content": bot_msg}
    ]
    mock_resp.error = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp

    mm.save_turn("user123", user_msg, bot_msg)
    mm.supabase.table.assert_called_once_with("chat_logs")
    mm.supabase.table.return_value.insert.assert_called_once_with([
        {"user_id": "user123", "role": "user", "content": user_msg},
        {"user_id": "user123", "role": "assistant", "content": bot_msg}
    ])

def test_save_turn_user_msg_exceeds_limit():
    from backend.memory import MAX_MESSAGE_LENGTH
    mm = MemoryManager()
    mm.supabase = MagicMock()

    user_msg = "a" * (MAX_MESSAGE_LENGTH + 1)
    bot_msg = "hello"
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", user_msg, bot_msg)
    
    assert "user123" not in str(exc.value)
    assert user_msg not in str(exc.value)
    mm.supabase.table.assert_not_called()

def test_save_turn_bot_msg_exceeds_limit():
    from backend.memory import MAX_MESSAGE_LENGTH
    mm = MemoryManager()
    mm.supabase = MagicMock()

    user_msg = "hello"
    bot_msg = "b" * (MAX_MESSAGE_LENGTH + 1)
    with pytest.raises(TurnPersistenceError) as exc:
        mm.save_turn("user123", user_msg, bot_msg)
    
    assert "user123" not in str(exc.value)
    assert bot_msg not in str(exc.value)
    mm.supabase.table.assert_not_called()

def test_user_history_isolation():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    mock_select = mm.supabase.table.return_value.select
    mock_eq = mock_select.return_value.eq
    mock_order1 = mock_eq.return_value.order
    mock_order2 = mock_order1.return_value.order
    mock_limit = mock_order2.return_value.limit
    
    mock_resp = MagicMock()
    mock_resp.data = [{"role": "user", "content": "hello"}]
    mock_resp.error = None
    mock_limit.return_value.execute.return_value = mock_resp
    
    history = mm.load_recent_history("userA")
    
    # Assert query filters strictly by userA
    mock_select.assert_called_with("role, content")
    mock_eq.assert_called_with("user_id", "userA")
    assert len(history) == 1

def test_deterministic_ordering_calls():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    mock_select = mm.supabase.table.return_value.select
    mock_eq = mock_select.return_value.eq
    mock_order1 = mock_eq.return_value.order
    mock_order2 = mock_order1.return_value.order
    mock_limit = mock_order2.return_value.limit
    
    mock_resp = MagicMock()
    mock_resp.data = [
        {"role": "assistant", "content": "reply2"},
        {"role": "user", "content": "msg2"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "msg1"}
    ]
    mock_resp.error = None
    mock_limit.return_value.execute.return_value = mock_resp
    
    history = mm.load_recent_history("userA", limit=4)
    
    # Assert query uses created_at and id as sorting tie-breakers in descending order
    mock_order1.assert_called_with("created_at", desc=True)
    mock_order2.assert_called_with("id", desc=True)
    
    # Assert returned history is reversed to chronological ascending order
    assert history == [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "msg2"},
        {"role": "assistant", "content": "reply2"}
    ]

def test_tied_timestamps_ordering():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    mock_select = mm.supabase.table.return_value.select
    mock_eq = mock_select.return_value.eq
    mock_order1 = mock_eq.return_value.order
    mock_order2 = mock_order1.return_value.order
    mock_limit = mock_order2.return_value.limit
    
    # Tied timestamps (identical created_at), ordered in DB by id desc:
    # 4 -> assistant reply2, 3 -> user msg2, 2 -> assistant reply1, 1 -> user msg1
    mock_resp = MagicMock()
    mock_resp.data = [
        {"id": 4, "role": "assistant", "content": "reply2", "created_at": "2026-07-12T22:00:00Z"},
        {"id": 3, "role": "user", "content": "msg2", "created_at": "2026-07-12T22:00:00Z"},
        {"id": 2, "role": "assistant", "content": "reply1", "created_at": "2026-07-12T22:00:00Z"},
        {"id": 1, "role": "user", "content": "msg1", "created_at": "2026-07-12T22:00:00Z"}
    ]
    mock_resp.error = None
    mock_limit.return_value.execute.return_value = mock_resp
    
    history = mm.load_recent_history("userA", limit=4)
    
    # Inversion in Python memory yields correct chronological insertion order: user -> assistant -> user -> assistant
    assert [h["content"] for h in history] == ["msg1", "reply1", "msg2", "reply2"]

def test_process_turn_awaits_save_turn_inside_lock():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.load_user_state = MagicMock(return_value={
            "emotional_state": _valid_legacy_emotion_dict(),
            "relationship_state": {}
        })
        engine.memory_manager.sync_state = MagicMock()
        engine._perceive = MagicMock(return_value={})
        
        mock_chat = MagicMock()
        mock_chat.choices = [MagicMock()]
        mock_chat.choices[0].message.content = "Bot reply"
        from unittest.mock import AsyncMock
        from backend.emotional_domain import AppraisalV1
        
        engine._appraise = AsyncMock(return_value=AppraisalV1.neutral())
        engine._generate = AsyncMock(return_value="Bot reply")

        save_turn_called = False
        def slow_save_turn(*args, **kwargs):
            nonlocal save_turn_called
            time.sleep(0.2)
            save_turn_called = True

        # Mock load_recent_history to return empty list
        engine.memory_manager.load_recent_history = MagicMock(return_value=[])

        with patch.object(engine.memory_manager, 'save_turn', side_effect=slow_save_turn):
            t = asyncio.create_task(engine.process_turn("user123", "Hello"))
            
            # Check lock is held and save_turn is not yet complete but started/awaited
            await asyncio.sleep(0.05)
            async with engine.lock_manager._dict_lock:
                assert "user123" in engine.lock_manager._locks
            
            await t
            assert save_turn_called
    asyncio.run(run_test())

def test_recreate_engine_preserves_context():
    async def run_test():
        engine1 = ConversationEngine()
        engine1._perceive = MagicMock(return_value={})
        engine1.memory_manager.load_user_state = MagicMock(return_value={
            "emotional_state": _valid_legacy_emotion_dict(),
            "relationship_state": {}
        })
        engine1.memory_manager.sync_state = MagicMock()
        
        # This test only calls get_context directly, not process_turn,
        # so the chat_completion mock is not needed. But keep it for safety.
        mock_chat = MagicMock()
        mock_chat.choices = [MagicMock()]
        mock_chat.choices[0].message.content = "Response"
        engine1.groq_manager.chat_completion = MagicMock(return_value=mock_chat)
        
        # Mock history returned from DB
        history_data = [{"role": "user", "content": "hi"}]
        engine1.memory_manager.load_recent_history = MagicMock(return_value=history_data)
        engine1.memory_manager.save_turn = MagicMock()
        
        context1 = await asyncio.to_thread(engine1.memory_manager.get_context, "user123", "new msg", {})
        
        # Recreate engine
        engine2 = ConversationEngine()
        engine2.memory_manager.load_recent_history = MagicMock(return_value=history_data)
        
        context2 = await asyncio.to_thread(engine2.memory_manager.get_context, "user123", "new msg", {})
        
        assert context1 == context2
    asyncio.run(run_test())

def test_get_context_propagates_load_failure():
    mm = MemoryManager()
    mm.supabase = MagicMock()
    
    # Mock load_recent_history to raise ContextLoadError
    mm.load_recent_history = MagicMock(side_effect=ContextLoadError("Database failure"))
    
    # Call get_context and assert it propagates the exception (fail closed)
    with pytest.raises(ContextLoadError):
        mm.get_context("user123", "hello", {})

def test_process_turn_fails_closed_on_load_failure():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.load_user_state = MagicMock(return_value={
            "emotional_state": _valid_legacy_emotion_dict(),
            "relationship_state": {}
        })
        engine.memory_manager.sync_state = MagicMock()
        engine._perceive = MagicMock()
        from unittest.mock import AsyncMock
        from backend.turn_execution import TurnExecutionError
        engine._appraise = AsyncMock(return_value=MagicMock())
        engine._generate = AsyncMock(return_value="Bot reply")
        engine.memory_manager.save_turn = MagicMock()
        
        # Mock load_recent_history to fail
        engine.memory_manager.load_recent_history = MagicMock(side_effect=ContextLoadError("DB error"))
        
        # Calling process_turn must propagate error (wrapped by run_blocking_read)
        with pytest.raises(TurnExecutionError):
            await engine.process_turn("user123", "Hello")
            
        # Verify that subsequent pipeline steps (save_turn, state sync) are NOT run
        engine.memory_manager.save_turn.assert_not_called()
        engine.memory_manager.sync_state.assert_not_called()
        
    asyncio.run(run_test())

def test_concurrent_process_turn_serialization():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.load_user_state = MagicMock(return_value={
            "emotional_state": _valid_legacy_emotion_dict(),
            "relationship_state": {}
        })
        engine.memory_manager.sync_state = MagicMock()
        engine._perceive = MagicMock(return_value={})
        
        from unittest.mock import AsyncMock
        from backend.emotional_domain import AppraisalV1
        engine._appraise = AsyncMock(return_value=AppraisalV1.neutral())
        engine._generate = AsyncMock(return_value="Bot reply")
        
        load_calls = []
        def mock_load(user_id, limit=10):
            load_calls.append((user_id, time.time()))
            return []
        engine.memory_manager.load_recent_history = mock_load

        save_started = threading.Event()
        save_proceed = threading.Event()
        save_finished = threading.Event()
        
        def slow_save(user_id, user_msg, bot_msg):
            save_started.set()
            save_proceed.wait(timeout=2.0)
            save_finished.set()

        engine.memory_manager.save_turn = slow_save

        # Start first turn
        t1 = asyncio.create_task(engine.process_turn("user123", "msg1"))
        
        # Wait until save_turn has started
        await asyncio.to_thread(save_started.wait, 2.0)
        assert save_started.is_set()
        
        # Start second turn for the SAME user
        t2 = asyncio.create_task(engine.process_turn("user123", "msg2"))
        
        # Give task 2 a moment to run and try to acquire the lock
        await asyncio.sleep(0.05)
        
        # load_recent_history should only have been called by task 1 so far
        assert len(load_calls) == 1
        assert load_calls[0][0] == "user123"
        
        # Release the first save_turn
        save_proceed.set()
        
        # Wait for both tasks to complete
        await t1
        await t2
        
        # load_recent_history is called twice in total
        assert len(load_calls) == 2
        # The second load call (load_calls[1][1]) happened after task 1 finished saving
        assert save_finished.is_set()
        
    asyncio.run(run_test())

def test_concurrent_different_users_not_blocked():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.load_user_state = MagicMock(return_value={
            "emotional_state": _valid_legacy_emotion_dict(),
            "relationship_state": {}
        })
        engine.memory_manager.sync_state = MagicMock()
        engine._perceive = MagicMock(return_value={})
        
        from unittest.mock import AsyncMock
        from backend.emotional_domain import AppraisalV1
        engine._appraise = AsyncMock(return_value=AppraisalV1.neutral())
        engine._generate = AsyncMock(return_value="Bot reply")
        
        load_calls = []
        def mock_load(user_id, limit=10):
            load_calls.append(user_id)
            return []
        engine.memory_manager.load_recent_history = mock_load

        save1_started = threading.Event()
        save1_proceed = threading.Event()
        
        def slow_save(user_id, user_msg, bot_msg):
            if user_id == "userA":
                save1_started.set()
                save1_proceed.wait(timeout=2.0)

        engine.memory_manager.save_turn = slow_save

        # Start task 1 for userA
        t1 = asyncio.create_task(engine.process_turn("userA", "msg1"))
        
        # Wait until userA's save starts
        await asyncio.to_thread(save1_started.wait, 2.0)
        assert save1_started.is_set()
        
        # Start task 2 for userB (different user)
        t2 = asyncio.create_task(engine.process_turn("userB", "msg2"))
        
        # Wait for task 2 to finish (it should NOT be blocked by userA's save!)
        await asyncio.wait_for(t2, timeout=2.0)
        
        # userB should have completed successfully
        assert "userB" in load_calls
        
        # Release userA
        save1_proceed.set()
        await t1
        assert "userA" in load_calls
        
    asyncio.run(run_test())

def test_repeated_cancellation_during_save_turn():
    async def run_test():
        engine = ConversationEngine()
        engine.memory_manager.load_user_state = MagicMock(return_value={
            "emotional_state": _valid_legacy_emotion_dict(),
            "relationship_state": {}
        })
        engine.memory_manager.sync_state = MagicMock()
        engine._perceive = MagicMock(return_value={})
        
        from unittest.mock import AsyncMock
        from backend.emotional_domain import AppraisalV1
        engine._appraise = AsyncMock(return_value=AppraisalV1.neutral())
        engine._generate = AsyncMock(return_value="Bot reply")
        
        engine.memory_manager.load_recent_history = MagicMock(return_value=[])

        save_started = threading.Event()
        save_proceed = threading.Event()
        save_finished = threading.Event()
        
        def slow_save(user_id, user_msg, bot_msg):
            save_started.set()
            save_proceed.wait(timeout=2.0)
            save_finished.set()

        engine.memory_manager.save_turn = slow_save

        # Start process_turn
        t1 = asyncio.create_task(engine.process_turn("user123", "msg1"))
        
        # Wait for save_turn to start
        await asyncio.to_thread(save_started.wait, 2.0)
        assert save_started.is_set()
        
        # Cancel task 1 repeatedly
        t1.cancel()
        t1.cancel()
        t1.cancel()
        
        await asyncio.sleep(0.05)
        assert not save_finished.is_set()
        
        # Release save_turn
        save_proceed.set()
        
        # t1 raises CancelledError
        with pytest.raises(asyncio.CancelledError):
            await t1
            
        # save_turn ran to completion
        assert save_finished.is_set()
        
        # Lock is released/cleaned up
        async with engine.lock_manager._dict_lock:
            assert "user123" not in engine.lock_manager._locks
            
    asyncio.run(run_test())
