from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from backend.admission import (
    ADMISSION_DECISIONS,
    ADMITTED,
    APPLICATION_RATE_LIMITED,
    INVALID_ADMISSION_INPUT,
    MESSAGE_HMAC_DOMAIN,
    NETWORK_HMAC_DOMAIN,
    NETWORK_RATE_LIMITED,
    REQUEST_ID_CONFLICT,
    REQUEST_REPLAY_UNAVAILABLE,
    UNKNOWN_NETWORK_IDENTITY,
    USER_DAILY_REQUEST_QUOTA_EXCEEDED,
    USER_DAILY_UNIT_QUOTA_EXCEEDED,
    USER_RATE_LIMITED,
    AdmissionConfigurationError,
    AdmissionRequest,
    AdmissionResult,
    AdmissionRuntimeConfig,
    AdmissionUnavailable,
    build_admission_request,
    compute_hmac_sha256,
    parse_admission_result,
    reserve_admission_sync,
    resolve_network_identity,
)
from backend.admission_contracts import RequestIdentity
from backend.turn_execution import (
    DeadlineExceeded,
    TurnBudget,
    TurnExecutionConfig,
    create_budget,
    run_blocking_write,
)

SECRET = "s" * 32
UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize("secret", [None, "", "   ", "short", 123, b"x" * 32])
def test_invalid_secret_fails_closed_without_exposure(secret):
    marker = "sensitive-secret-marker"
    with pytest.raises(AdmissionConfigurationError) as exc_info:
        AdmissionRuntimeConfig.from_values(secret)
    assert str(exc_info.value) == "invalid_admission_configuration"
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)


def test_valid_config_is_frozen_and_redacts_secret():
    config = AdmissionRuntimeConfig.from_values(
        SECRET,
        "10.0.0.0/8, 2001:db8::/32",
    )
    assert len(config.secret_bytes) == 32
    assert len(config.trusted_proxy_networks) == 2
    assert SECRET not in repr(config)
    with pytest.raises(Exception):
        config.secret_bytes = b"changed"


@pytest.mark.parametrize("cidrs", ["invalid", "10.0.0.0/8,", ",10.0.0.0/8", 42])
def test_invalid_proxy_config_fails_closed(cidrs):
    with pytest.raises(AdmissionConfigurationError):
        AdmissionRuntimeConfig.from_values(SECRET, cidrs)


def test_hmac_is_exact_utf8_and_domain_separated():
    secret = SECRET.encode("utf-8")
    text = "Olá, Katherine 🌙"
    expected = hmac.new(
        secret,
        b"message\x00" + text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    message_digest = compute_hmac_sha256(secret, MESSAGE_HMAC_DOMAIN, text)
    network_digest = compute_hmac_sha256(secret, NETWORK_HMAC_DOMAIN, text)
    assert message_digest == expected
    assert message_digest != network_digest
    assert len(message_digest) == 64
    assert message_digest == message_digest.lower()


def test_untrusted_peer_ignores_forwarded_header():
    config = AdmissionRuntimeConfig.from_values(SECRET, "10.0.0.0/8")
    assert resolve_network_identity(
        "203.0.113.8",
        "198.51.100.7",
        config.trusted_proxy_networks,
    ) == "203.0.113.8"


def test_trusted_proxy_chain_is_walked_from_right_to_left():
    config = AdmissionRuntimeConfig.from_values(
        SECRET,
        "10.0.0.0/8,192.168.0.0/16",
    )
    assert resolve_network_identity(
        "10.0.0.5",
        "198.51.100.9, 192.168.1.4, 10.0.0.4",
        config.trusted_proxy_networks,
    ) == "198.51.100.9"


def test_all_trusted_proxy_chain_uses_leftmost_address():
    config = AdmissionRuntimeConfig.from_values(SECRET, "10.0.0.0/8")
    assert resolve_network_identity(
        "10.0.0.5",
        "10.1.1.1, 10.2.2.2",
        config.trusted_proxy_networks,
    ) == "10.1.1.1"


@pytest.mark.parametrize(
    ("peer", "header"),
    [
        (None, None),
        ("invalid", "198.51.100.1"),
        ("10.0.0.5", None),
        ("10.0.0.5", ""),
        ("10.0.0.5", "198.51.100.1,,10.0.0.4"),
        ("10.0.0.5", "not-an-ip"),
    ],
)
def test_invalid_or_ambiguous_network_input_uses_unknown(peer, header):
    config = AdmissionRuntimeConfig.from_values(SECRET, "10.0.0.0/8")
    assert (
        resolve_network_identity(peer, header, config.trusted_proxy_networks)
        == UNKNOWN_NETWORK_IDENTITY
    )


def test_ipv6_is_canonicalised():
    config = AdmissionRuntimeConfig.from_values(SECRET)
    assert resolve_network_identity(
        "2001:0db8:0000:0000:0000:0000:0000:0001",
        None,
        config.trusted_proxy_networks,
    ) == "2001:db8::1"


def test_build_request_contains_only_rpc_contract_and_no_raw_payload():
    config = AdmissionRuntimeConfig.from_values(SECRET)
    request = build_admission_request(
        user_id="user-a",
        request_identity=RequestIdentity(UUID.upper()),
        message="raw-sensitive-message",
        network_identity="203.0.113.5",
        config=config,
    )
    assert request.request_id == UUID
    assert request.estimated_units == len("raw-sensitive-message".encode("utf-8"))
    assert set(request.rpc_params()) == {
        "p_user_id",
        "p_request_id",
        "p_message_hmac_sha256",
        "p_network_hmac_sha256",
        "p_estimated_units",
    }
    assert "raw-sensitive-message" not in repr(request)
    assert "203.0.113.5" not in repr(request)
    assert SECRET not in repr(request)


@pytest.mark.parametrize(
    ("decision", "retry"),
    [
        (ADMITTED, 0),
        (REQUEST_REPLAY_UNAVAILABLE, 0),
        (REQUEST_ID_CONFLICT, 0),
        (INVALID_ADMISSION_INPUT, 0),
        (USER_RATE_LIMITED, 60),
        (NETWORK_RATE_LIMITED, 60),
        (APPLICATION_RATE_LIMITED, 60),
        (USER_DAILY_REQUEST_QUOTA_EXCEEDED, 86400),
        (USER_DAILY_UNIT_QUOTA_EXCEEDED, 86400),
    ],
)
def test_parse_admission_result_accepts_only_exact_contract(decision, retry):
    result = parse_admission_result(
        [{"decision": decision, "retry_after_seconds": retry}]
    )
    assert result.decision == decision
    assert result.retry_after_seconds == retry
    assert decision in ADMISSION_DECISIONS


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        [{"decision": ADMITTED, "retry_after_seconds": 0}, {}],
        {},
        [{"decision": ADMITTED}],
        [{"decision": ADMITTED, "retry_after_seconds": 0, "extra": True}],
        [{"decision": "unknown", "retry_after_seconds": 0}],
        [{"decision": ADMITTED, "retry_after_seconds": True}],
        [{"decision": ADMITTED, "retry_after_seconds": -1}],
        [{"decision": ADMITTED, "retry_after_seconds": 60}],
        [{"decision": USER_RATE_LIMITED, "retry_after_seconds": 1}],
    ],
)
def test_parse_admission_result_rejects_malformed_payload(payload):
    with pytest.raises(AdmissionUnavailable):
        parse_admission_result(payload)


class FakeRpcBuilder:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return SimpleNamespace(data=self._data)


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return FakeRpcBuilder(self.data)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_id": "   "},
        {"request_id": "not-a-uuid"},
        {"message_hmac_sha256": "A" * 64},
        {"network_hmac_sha256": "z" * 64},
        {"estimated_units": 0},
        {"estimated_units": True},
    ],
)
def test_direct_admission_request_construction_fails_closed(kwargs):
    values = {
        "user_id": "user-a",
        "request_id": UUID,
        "message_hmac_sha256": "a" * 64,
        "network_hmac_sha256": "b" * 64,
        "estimated_units": 7,
    }
    values.update(kwargs)
    with pytest.raises(AdmissionUnavailable):
        AdmissionRequest(**values)


@pytest.mark.parametrize(
    ("decision", "retry"),
    [("unknown", 0), (ADMITTED, 60), (USER_RATE_LIMITED, True)],
)
def test_direct_admission_result_construction_fails_closed(decision, retry):
    with pytest.raises(AdmissionUnavailable):
        AdmissionResult(decision, retry)


def test_sync_adapter_calls_exact_rpc_and_params():
    request = AdmissionRequest(
        user_id="user-a",
        request_id=UUID,
        message_hmac_sha256="a" * 64,
        network_hmac_sha256="b" * 64,
        estimated_units=7,
    )
    client = FakeClient([{"decision": ADMITTED, "retry_after_seconds": 0}])
    result = reserve_admission_sync(client, request)
    assert result.decision == ADMITTED
    assert client.calls == [("reserve_admission", request.rpc_params())]


def test_sync_adapter_sanitises_upstream_exception():
    marker = "upstream-sensitive-marker"

    class FailingClient:
        def rpc(self, _name, _params):
            raise RuntimeError(marker)

    with pytest.raises(AdmissionUnavailable) as exc_info:
        reserve_admission_sync(
            FailingClient(),
            AdmissionRequest("user", UUID, "a" * 64, "b" * 64, 1),
        )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)


def test_write_cancellation_drains_reservation_worker():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_reservation():
        started.set()
        release.wait(timeout=5)
        finished.set()
        return "done"

    async def scenario():
        budget = create_budget(TurnExecutionConfig.defaults())
        task = asyncio.create_task(
            run_blocking_write(
                "reserve_admission",
                budget,
                5.0,
                blocking_reservation,
            )
        )
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


def test_expired_budget_does_not_start_reservation_worker():
    called = False

    def worker():
        nonlocal called
        called = True

    async def scenario():
        budget = TurnBudget(deadline=0.0, reserve=0.0, now_provider=lambda: 1.0)
        with pytest.raises(DeadlineExceeded):
            await run_blocking_write("reserve_admission", budget, 5.0, worker)

    asyncio.run(scenario())
    assert called is False


def test_module_import_has_no_env_network_or_heavy_import_side_effects():
    script = r'''
import builtins
import os
import socket
import sys

class BombEnvironment(dict):
    def __getitem__(self, key):
        raise AssertionError("environment read")
    def get(self, key, default=None):
        raise AssertionError("environment read")

os.environ = BombEnvironment()
os.getenv = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("getenv"))
socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket"))
socket.create_connection = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network"))

blocked = {
    "fastapi", "pydantic", "supabase", "postgrest", "groq",
    "sentence_transformers", "backend.engine", "backend.memory",
}
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name in blocked or any(name.startswith(item + ".") for item in blocked):
        raise AssertionError("heavy import: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import

import backend.admission
print("ok")
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = os.getcwd()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
