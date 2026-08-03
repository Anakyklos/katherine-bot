from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import Request

import backend.main as main
from backend.admission import (
    AdmissionConfigurationError,
    AdmissionRuntimeConfig,
    AdmissionUnavailable,
    build_admission_request,
)
from backend.admission_contracts import RequestIdentity

UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize(
    "secret_bytes",
    [
        b" " * 32,
        b"\xff" * 32,
    ],
)
def test_direct_config_constructor_rejects_invalid_utf8_or_whitespace(secret_bytes):
    with pytest.raises(AdmissionConfigurationError):
        AdmissionRuntimeConfig(secret_bytes=secret_bytes)


@pytest.mark.parametrize(
    "network_identity",
    [
        "",
        "not-an-ip",
        "2001:0db8:0000:0000:0000:0000:0000:0001",
        " 203.0.113.1",
    ],
)
def test_request_builder_rejects_noncanonical_network_identity(network_identity):
    with pytest.raises(AdmissionUnavailable):
        build_admission_request(
            user_id="user-a",
            request_identity=RequestIdentity(UUID),
            message="hello",
            network_identity=network_identity,
            config=AdmissionRuntimeConfig.from_values("s" * 32),
        )


def test_request_builder_accepts_unknown_network_bucket():
    request = build_admission_request(
        user_id="user-a",
        request_identity=RequestIdentity(UUID),
        message="hello",
        network_identity="unknown",
        config=AdmissionRuntimeConfig.from_values("s" * 32),
    )
    assert len(request.network_hmac_sha256) == 64


def test_endpoint_cancellation_drains_reservation_and_never_calls_engine(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class FakeEngine:
        def __init__(self):
            self.memory_manager = SimpleNamespace(supabase=object())
            self.calls = []

        async def process_turn(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("engine must not run after cancelled admission")

    fake_engine = FakeEngine()
    monkeypatch.setattr(main, "engine", fake_engine)
    monkeypatch.setattr(
        main,
        "_admission_config",
        AdmissionRuntimeConfig.from_values("s" * 32),
    )

    def blocking_reservation(_client, _request):
        started.set()
        release.wait(timeout=5)
        completed.set()
        return None

    monkeypatch.setattr(main, "reserve_admission_sync", blocking_reservation)

    async def scenario():
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/chat",
                "headers": [],
                "client": ("203.0.113.8", 12345),
            }
        )
        task = asyncio.create_task(
            main.chat_endpoint(
                main.ChatInput(request_id=UUID, message="hello"),
                request,
                current_user=SimpleNamespace(id="user-a"),
            )
        )
        started_ok = await asyncio.to_thread(started.wait, 2)
        assert started_ok

        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert completed.is_set()
    assert fake_engine.calls == []
