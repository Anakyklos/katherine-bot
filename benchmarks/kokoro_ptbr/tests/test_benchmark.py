from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


SCRIPT = Path(__file__).parents[1] / "benchmark.py"
SPEC = importlib.util.spec_from_file_location("kokoro_ptbr_benchmark", SCRIPT)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def candidate_args(**overrides):
    spec = benchmark._read_spec()
    values = {
        "model_id": spec["model_id"],
        "model_revision": spec["model_revision"],
        "voice": spec["voice"],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_versioned_synthetic_corpus_has_twenty_items_and_short_responses():
    items = benchmark.load_corpus(SCRIPT.parent / "corpus.jsonl")
    assert len(items) == 20
    assert [item["text"] for item in items[:3]] == ["Sim.", "Entendi.", "Claro!"]
    assert {"saudacao", "pergunta", "numeros", "horario", "data", "nomes_proprios", "siglas", "ingles_misturado", "termo_tecnico", "frase_longa"} <= {
        item["category"] for item in items
    }


def test_run_requires_explicit_model_and_all_directories():
    parser = benchmark.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--model-id", "hexgrad/Kokoro-82M", "--limit", "0"])


def test_candidate_rejects_unpinned_model_revision_or_voice():
    for overrides in ({"model_id": "other/model"}, {"model_revision": "main"}, {"voice": "af_heart"}):
        with pytest.raises(benchmark.BenchmarkError) as raised:
            benchmark._validate_candidate(candidate_args(**overrides))
        assert raised.value.code == "model_config_mismatch"


def test_download_requires_explicit_opt_in_without_creating_directories(tmp_path):
    model_dir = tmp_path / "weights"
    cache_dir = tmp_path / "hf-cache"
    args = candidate_args(allow_downloads=False, model_dir=model_dir, cache_dir=cache_dir)
    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark.fetch_assets(args)
    assert raised.value.code == "downloads_not_authorized"
    assert not model_dir.exists()
    assert not cache_dir.exists()


def test_explicit_download_keeps_hub_and_xet_caches_inside_configured_directory(tmp_path, monkeypatch):
    payload = b"explicit public test artifact"
    spec = {
        "model_id": "hexgrad/Kokoro-82M",
        "model_revision": "f3ff3571791e39611d31c381e3a41a3af07b4987",
        "model_release": "v1.0",
        "voice": "pf_dora",
        "license": "Apache-2.0",
        "sample_rate_hz": 24_000,
        "assets": [{"path": "weights.bin", "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(benchmark, "SPEC_PATH", spec_path)
    for key in ("HF_HOME", "HF_HUB_CACHE", "HF_XET_CACHE", "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_XET"):
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.delenv(key, raising=False)
    cache_dir = tmp_path / "explicit-cache"
    model_dir = tmp_path / "model"
    observed = {}

    def fake_download(**kwargs):
        observed.update(kwargs)
        source = cache_dir / "source.bin"
        source.write_bytes(payload)
        return str(source)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=fake_download))
    args = argparse.Namespace(
        model_id=spec["model_id"],
        model_revision=spec["model_revision"],
        voice=spec["voice"],
        model_dir=model_dir,
        cache_dir=cache_dir,
        allow_downloads=True,
    )
    benchmark.fetch_assets(args)
    assert (model_dir / "weights.bin").read_bytes() == payload
    assert observed["token"] is False
    assert observed["revision"] == spec["model_revision"]
    assert Path(os.environ["HF_HOME"]).is_relative_to(cache_dir)
    assert Path(os.environ["HF_XET_CACHE"]).is_relative_to(cache_dir)
    assert Path(os.environ["HF_HUB_CACHE"]).is_relative_to(cache_dir)
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"


def test_missing_and_corrupt_assets_fail_closed(tmp_path):
    model_dir = tmp_path / "weights"
    model_dir.mkdir()
    with pytest.raises(benchmark.BenchmarkError) as missing:
        benchmark.verify_assets(model_dir)
    assert missing.value.code == "asset_missing"

    (model_dir / "config.json").write_text("bad", encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError) as corrupt:
        benchmark.verify_assets(model_dir)
    assert corrupt.value.code == "asset_corrupt"


def test_run_missing_assets_does_not_start_worker_or_download(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("worker must not start when pinned assets are missing")

    monkeypatch.setattr(benchmark.subprocess, "Popen", forbidden)
    args = candidate_args(
        model_dir=tmp_path / "missing-model",
        corpus=SCRIPT.parent / "corpus.jsonl",
        output_dir=tmp_path / "output",
        cache_dir=tmp_path / "hf-cache",
        uv_cache_dir=tmp_path / "uv-cache",
        limit=1,
        idle_seconds=0,
        threads=1,
    )
    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark.run_benchmark(args)
    assert raised.value.code == "asset_missing"
    assert not args.output_dir.exists()


def test_corpus_rejects_invalid_duplicate_and_oversized_entries(tmp_path):
    corpus = tmp_path / "invalid.jsonl"
    corpus.write_text('{"id":"same","category":"x","text":"ok"}\n{"id":"same","category":"x","text":"again"}\n', encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError) as duplicate:
        benchmark.load_corpus(corpus)
    assert duplicate.value.code == "corpus_invalid"

    corpus.write_text(json.dumps({"id": "long", "category": "x", "text": "a" * 401}) + "\n", encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError) as oversized:
        benchmark.load_corpus(corpus)
    assert oversized.value.code == "input_too_long"


def test_worker_is_offline_and_does_not_inherit_hub_credentials(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "must-not-be-inherited-or-printed")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "must-not-be-inherited-or-printed")
    env = benchmark.worker_environment(3)
    assert "HF_TOKEN" not in env
    assert "HUGGING_FACE_HUB_TOKEN" not in env
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["CUDA_VISIBLE_DEVICES"] == ""
    assert env["OMP_NUM_THREADS"] == "3"


def test_runtime_is_built_from_explicit_local_weight_and_voice_paths(tmp_path, monkeypatch):
    observed = {}

    class FakeModel:
        def __init__(self, **kwargs):
            observed["model_args"] = kwargs

        def to(self, device):
            observed["device"] = device
            return self

        def eval(self):
            return self

    class FakePipeline:
        def __init__(self, **kwargs):
            observed["pipeline_args"] = kwargs

        def load_voice(self, path):
            observed["voice_path"] = path
            return "local-voice"

    fake_hub = SimpleNamespace(hf_hub_download=lambda **_kwargs: pytest.fail("download attempted"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    paths = {
        "config.json": tmp_path / "config.json",
        "kokoro-v1_0.pth": tmp_path / "kokoro-v1_0.pth",
        "voices/pf_dora.pt": tmp_path / "voices" / "pf_dora.pt",
    }
    model, pipeline, voice = benchmark._build_local_pipeline(
        "hexgrad/Kokoro-82M",
        "pf_dora",
        paths,
        SimpleNamespace(device=lambda name: name),
        FakeModel,
        FakePipeline,
    )
    assert model is not None and pipeline is not None and voice == "local-voice"
    assert observed["model_args"]["config"] == str(paths["config.json"])
    assert observed["model_args"]["model"] == str(paths["kokoro-v1_0.pth"])
    assert observed["device"] == "cpu"
    assert observed["pipeline_args"]["lang_code"] == "p"
    assert observed["voice_path"] == str(paths["voices/pf_dora.pt"])


@pytest.mark.parametrize(
    ("pipeline", "expected_code"),
    [
        (SimpleNamespace(g2p=lambda _text: (_ for _ in ()).throw(RuntimeError("secret detail"))), "phonemizer_failed"),
        (SimpleNamespace(g2p=lambda _text: ("", None)), "phonemizer_empty"),
        (SimpleNamespace(g2p=lambda _text: ("x" * 511, None)), "phoneme_limit"),
    ],
)
def test_phonemizer_errors_are_classified_and_sanitized(pipeline, expected_code):
    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark._synthesize_text(pipeline, object(), "Oi.")
    assert raised.value.code == expected_code
    assert "secret detail" not in str(raised.value)


def test_inference_error_is_sanitized_and_observable():
    class FailedPipeline:
        @staticmethod
        def g2p(_text):
            return "oj", None

        @staticmethod
        def generate_from_tokens(*_args, **_kwargs):
            raise RuntimeError("sensitive upstream details")

    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark._synthesize_text(FailedPipeline(), object(), "Oi.")
    assert raised.value.code == "synthesis_failed"
    assert "sensitive upstream details" not in str(raised.value)


def test_empty_inference_result_is_rejected():
    class EmptyPipeline:
        @staticmethod
        def g2p(_text):
            return "oj", None

        @staticmethod
        def generate_from_tokens(*_args, **_kwargs):
            yield SimpleNamespace(audio=None)

    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark._synthesize_text(EmptyPipeline(), object(), "Oi.")
    assert raised.value.code == "audio_invalid"


def test_missing_audio_device_is_observable_but_does_not_block_wav_synthesis(tmp_path):
    result = benchmark._audio_device_probe(tmp_path / "no-device")
    assert result["device_nodes_detected"] is False
    assert result["playback_attempted_by_benchmark"] is False
    assert result["synthesis_requires_audio_device"] is False
    assert result["human_listening"] == "UNVERIFIED"


def test_cpu_percentage_is_normalized_to_one_logical_cpu():
    assert benchmark._mean_cpu_percent(2.0, 0.5) == 400.0
    assert benchmark._mean_cpu_percent(0.0, 0.0) == 0.0


def test_atomic_wav_write_produces_valid_mono_pcm_and_hash(tmp_path):
    path = tmp_path / "short.wav"
    waveform = np.linspace(-0.5, 0.5, 240, dtype=np.float32)
    info = benchmark._atomic_write_wav(path, waveform, sf, np, 24_000)
    checked = sf.info(path)
    assert checked.format == "WAV"
    assert checked.subtype == "PCM_16"
    assert checked.samplerate == 24_000
    assert checked.channels == 1
    assert info["frames"] == 240
    assert len(info["wav_sha256"]) == 64
    assert list(tmp_path.glob(".kokoro-bench-*.partial*")) == []


def test_atomic_wav_write_cleans_partial_file_after_failure(tmp_path):
    class FailingSoundFile:
        @staticmethod
        def write(path, *_args, **_kwargs):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("sensitive upstream exception")

    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark._atomic_write_wav(tmp_path / "bad.wav", np.ones(64, dtype=np.float32), FailingSoundFile, np, 24_000)
    assert raised.value.code == "audio_write_failed"
    assert "sensitive upstream exception" not in str(raised.value)
    assert list(tmp_path.iterdir()) == []


def test_invalid_audio_is_rejected_before_a_file_is_left(tmp_path):
    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark._atomic_write_wav(tmp_path / "nan.wav", np.array([np.nan], dtype=np.float32), sf, np, 24_000)
    assert raised.value.code == "audio_invalid"
    assert list(tmp_path.iterdir()) == []


def test_metrics_schema_requires_structured_timing_resources_and_samples():
    metrics = {
        "schema_version": 1,
        "status": "completed",
        "error_code": None,
        "host": {},
        "candidate": {"model_id": "x", "model_revision": "y", "voice": "z", "inference_device": "cpu", "torch_threads": 4, "sample_rate_hz": 24000, "assets": []},
        "timing": {"process_spawn_to_model_ready_ms": 1, "model_load_ms": 1, "model_load_cpu_seconds": 1, "loaded_idle_cpu_seconds": 0, "loaded_idle_cpu_mean_percent": 0, "first_inference_after_model_load_ms": 1, "process_spawn_to_first_wav_ready_ms": 1, "synthesis_cpu_seconds_total": 1, "synthesis_cpu_mean_percent": 1, "real_time_factor": 1, "shutdown_phase_to_process_exit_ms": 1},
        "resources": {"rss_baseline_kib": 1, "rss_peak_kib": 2, "rss_incremental_peak_kib": 1, "pss_baseline_kib": 1, "pss_peak_kib": 2, "pss_incremental_peak_kib": 1, "phases": {}},
        "disk": {"model_assets_apparent_bytes": 1, "huggingface_cache_apparent_bytes": 1, "uv_cache_apparent_bytes": 1, "installed_benchmark_environment_apparent_bytes": 1, "generated_wav_apparent_bytes": 1},
        "audio_output": {},
        "samples": [{"id": "short", "audio_file": "short.wav", "audio_duration_seconds": 1, "generation_ms": 1, "cpu_seconds": 1, "cpu_mean_percent": 100, "real_time_factor": 1, "wav_sha256": "0" * 64}],
        "process": {},
        "method": {},
    }
    benchmark.validate_metrics_schema(metrics)
    del metrics["resources"]["pss_peak_kib"]
    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark.validate_metrics_schema(metrics)
    assert raised.value.code == "worker_protocol_failed"


def test_output_directory_refuses_existing_files_and_overlapping_assets(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    cache_dir = tmp_path / "hf"
    uv_cache_dir = tmp_path / "uv"
    output = tmp_path / "output"
    output.mkdir()
    (output / "previous.wav").write_bytes(b"keep")
    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark._prepare_output_dir(output, model_dir, cache_dir, uv_cache_dir)
    assert raised.value.code == "output_invalid"
    assert (output / "previous.wav").read_bytes() == b"keep"

    with pytest.raises(benchmark.BenchmarkError) as overlap:
        benchmark._prepare_output_dir(model_dir / "runs", model_dir, cache_dir, uv_cache_dir)
    assert overlap.value.code == "path_invalid"


def test_cleanup_removes_only_harness_partial_files(tmp_path):
    partial = tmp_path / ".kokoro-bench-short-abc.partial.wav"
    complete = tmp_path / "short.wav"
    unrelated = tmp_path / "notes.partial"
    partial.write_bytes(b"partial")
    complete.write_bytes(b"complete")
    unrelated.write_bytes(b"keep")
    assert benchmark.cleanup_partial_outputs(tmp_path) == 1
    assert not partial.exists()
    assert complete.exists()
    assert unrelated.exists()


def test_termination_reaps_worker_process_group():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert benchmark._terminate_process(child, grace_seconds=1.0) is True
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_cli_reports_missing_assets_without_paths_or_upstream_details(tmp_path, capsys):
    spec = benchmark._read_spec()
    result = benchmark.main(
        [
            "verify-assets",
            "--model-id", spec["model_id"],
            "--model-revision", spec["model_revision"],
            "--voice", spec["voice"],
            "--model-dir", str(tmp_path / "missing-secret-path"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "asset_missing" in captured.err
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err
