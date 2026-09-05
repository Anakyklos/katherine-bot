#!/usr/bin/env python3
"""Deterministic package-level proof of the real Groq adapter path (#338).

The probe uses the installed runtime and adapter, but replaces only the SDK
transport at the existing ``GroqClientManager.async_client_factory`` seam.
It therefore exercises:

    CompanionRuntime -> LanguageModel contract -> GroqLanguageModel
    -> GroqClientManager -> controlled chat completion transport

No network request or real credential is used. The key is retained only by the
real manager and is never included in the result or logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

APP_ROOT = Path(os.environ.get("KAT_APP_ROOT", "/usr/lib/katherine"))
if not (APP_ROOT / "backend").is_dir():
    APP_ROOT = Path("/install-root/usr/lib/katherine")
if not (APP_ROOT / "backend").is_dir():
    APP_ROOT = Path(__file__).resolve().parent.parent
if not (APP_ROOT / "backend").is_dir():
    raise RuntimeError("installed Katherine package tree not found")

sys.path.insert(0, str(APP_ROOT / "vendor"))
sys.path.insert(0, str(APP_ROOT))

from backend.companion_runtime import CompanionRuntime  # noqa: E402
from backend.groq_language_model import GroqLanguageModel  # noqa: E402
from backend.groq_manager import GroqClientManager  # noqa: E402
from backend.provider_models import FAST_MODEL_ID, MAIN_MODEL_ID  # noqa: E402
from backend.turn_execution import GroqCallParams  # noqa: E402

EXPECTED_RESPONSE = "packaged provider response"
PROBE_KEY = "package-provider-probe-key"
APPRAISAL_RESPONSE = json.dumps(
    {
        "valence": 0.0,
        "arousal_shift": 0.0,
        "dominance_shift": 0.0,
        "triggered_emotions": {},
    }
)


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(content=content))
        ]


class _ControlledCompletions:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def create(self, **kwargs: Any) -> _Completion:
        self._calls.append(kwargs)
        # Keep the controlled call long enough for the process sampler to
        # observe the effective provider turn without using a network.
        await asyncio.sleep(0.02)
        call_number = len(self._calls)
        if call_number == 1:
            return _Completion(APPRAISAL_RESPONSE)
        if call_number == 2:
            return _Completion(EXPECTED_RESPONSE)
        raise AssertionError("provider fallback or unexpected third call")


class _ControlledClient:
    def __init__(self, key: str, calls: list[dict[str, Any]]) -> None:
        self._key = key
        self.chat = SimpleNamespace(
            completions=_ControlledCompletions(calls)
        )
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _proc_memory() -> tuple[int, int]:
    rss = 0
    pss = 0
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        pass
    try:
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                pss = int(line.split()[1])
                break
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        pass
    return rss, pss


def run_probe(storage_path: Path) -> dict[str, Any]:
    """Run one real local turn through the packaged provider adapter."""
    calls: list[dict[str, Any]] = []
    clients: list[_ControlledClient] = []

    def controlled_client_factory(key: str) -> _ControlledClient:
        client = _ControlledClient(key, calls)
        clients.append(client)
        return client

    # This is the real manager class. Only its network-client factory is
    # controlled, which is the same injection seam used by manager tests.
    manager = GroqClientManager(
        keys=[PROBE_KEY],
        async_client_factory=controlled_client_factory,
        groq_params=GroqCallParams(
            max_attempts=1,
            provider_attempt_timeout=5.0,
            connect_timeout=5.0,
        ),
    )
    model = GroqLanguageModel(manager)
    runtime = CompanionRuntime(
        storage_path=storage_path,
        # The runtime receives the real LanguageModel adapter through its
        # canonical factory seam, not a scripted provider implementation.
        language_model_factory=lambda: model,
        provider_configured_probe=manager.is_configured,
    )
    try:
        readiness = runtime.runtime_state()
        provider_rss_before, provider_pss_before = _proc_memory()
        samples: list[tuple[int, int]] = []
        stop_sampling = threading.Event()

        def sample_memory() -> None:
            while not stop_sampling.wait(0.01):
                samples.append(_proc_memory())

        sampler = threading.Thread(target=sample_memory, daemon=True)
        sampler.start()
        provider_started = time.perf_counter()
        provider_cpu_started = time.process_time()
        result = asyncio.run(
            runtime.commit_turn_async(
                request_id="packaged-provider-probe",
                message="deterministic provider probe",
            )
        )
        provider_cpu_elapsed = time.process_time() - provider_cpu_started
        provider_elapsed = time.perf_counter() - provider_started
        stop_sampling.set()
        sampler.join(timeout=1)
        provider_memory_after = _proc_memory()
        memory_samples = [
            (provider_rss_before, provider_pss_before),
            *samples,
            provider_memory_after,
        ]
        selection = model.describe()
        payload = {
            "success": result.success,
            "response": result.response,
            "provider": selection.provider,
            "models": [
                selection.fast_model_id,
                selection.main_model_id,
            ],
            "calls": len(calls),
            "fallback": len(calls) > 2,
            "key_echoed": PROBE_KEY in json.dumps(
                {
                    "readiness": readiness,
                    "result": result.to_payload(),
                },
                sort_keys=True,
            ),
            "clients_closed": all(client.closed for client in clients),
            "provider_turn_duration_ms": round(provider_elapsed * 1000, 1),
            "provider_turn_cpu_percent": round(
                provider_cpu_elapsed / max(provider_elapsed, 0.001) * 100,
                2,
            ),
            "provider_turn_rss_before_kib": provider_rss_before,
            "provider_turn_rss_peak_kib": max(item[0] for item in memory_samples),
            "provider_turn_pss_peak_kib": max(item[1] for item in memory_samples),
        }
        return payload
    finally:
        runtime.close()


def main() -> int:
    storage_path = Path(
        os.environ.get(
            "KAT_PROVIDER_SMOKE_DB",
            "/tmp/katherine-provider-smoke/katherine.db",
        )
    )
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_probe(storage_path)
    print(json.dumps(result, sort_keys=True))
    return int(
        not (
            result["success"] is True
            and result["response"] == EXPECTED_RESPONSE
            and result["provider"] == "groq"
            and result["models"] == [FAST_MODEL_ID, MAIN_MODEL_ID]
            and result["calls"] == 2
            and result["fallback"] is False
            and result["key_echoed"] is False
            and result["clients_closed"] is True
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
