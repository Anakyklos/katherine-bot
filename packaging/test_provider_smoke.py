from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("provider_smoke.py")
spec = importlib.util.spec_from_file_location("katherine_provider_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
provider_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider_smoke)


def test_packaged_provider_probe_crosses_real_adapter_and_runtime(tmp_path: Path) -> None:
    result = provider_smoke.run_probe(tmp_path / "katherine.db")

    assert result["success"] is True
    assert result["response"] == provider_smoke.EXPECTED_RESPONSE
    assert result["provider"] == "groq"
    assert result["calls"] == 2
    assert result["models"] == [provider_smoke.FAST_MODEL_ID, provider_smoke.MAIN_MODEL_ID]
    assert result["fallback"] is False
    assert result["key_echoed"] is False
    assert result["provider_turn_duration_ms"] >= 0
    assert result["provider_turn_rss_peak_kib"] >= result["provider_turn_rss_before_kib"]
    assert result["provider_turn_cpu_percent"] >= 0
