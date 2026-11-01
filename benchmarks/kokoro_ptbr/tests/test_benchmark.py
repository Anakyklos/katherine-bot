from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
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


def _make_test_sample(tmp_path: Path, sample_id: str, frame_count: int = 4) -> dict:
    source_dir = tmp_path / "sample-source"
    source_dir.mkdir(exist_ok=True)
    source_path = source_dir / f"{sample_id}.wav"
    wav_info = benchmark._atomic_write_wav(
        source_path,
        np.resize(np.array([0.1, -0.1, 0.2, -0.2], dtype=np.float32), frame_count),
        sf,
        np,
        benchmark.SAMPLE_RATE_HZ,
    )
    generation_ms = 4.0
    return {
        "event": "sample_complete",
        "id": sample_id,
        "category": "synthetic",
        "text_chars": 4,
        "phoneme_chars": 4,
        "phonemization_ms": 1.0,
        "inference_ms": 3.0,
        "generation_ms": generation_ms,
        "cpu_seconds": 0.004,
        "cpu_mean_percent": 100.0,
        "audio_duration_seconds": round(wav_info["audio_duration_seconds"], 6),
        "real_time_factor": round(generation_ms / 1000 / wav_info["audio_duration_seconds"], 6),
        **wav_info,
    }


def _run_fake_worker(
    tmp_path: Path,
    monkeypatch,
    sample_ids: list[str],
    worker_count: int,
    *,
    missing_wavs: set[str] | None = None,
    corrupt_wavs: set[str] | None = None,
    frame_mismatches: dict[str, tuple[int, int]] | None = None,
):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    asset = model_dir / "placeholder.asset"
    asset.write_bytes(b"test asset")
    asset_paths = {item["path"]: asset for item in benchmark._read_spec()["assets"]}
    monkeypatch.setattr(benchmark, "verify_assets", lambda _model_dir: asset_paths)

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        json.dumps({"id": "sample_a", "category": "synthetic", "text": "A."}) + "\n"
        + json.dumps({"id": "sample_b", "category": "synthetic", "text": "B."}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = candidate_args(
        model_dir=model_dir,
        corpus=corpus,
        output_dir=output_dir,
        cache_dir=tmp_path / "hf-cache",
        uv_cache_dir=tmp_path / "uv-cache",
        limit=None,
        idle_seconds=0,
        threads=1,
    )
    frame_mismatches = frame_mismatches or {}
    sample_events = [
        _make_test_sample(tmp_path, sample_id, frame_mismatches.get(sample_id, (4, 4))[0])
        for sample_id in sample_ids
    ]
    for event in sample_events:
        if event["id"] in frame_mismatches:
            _actual_frames, reported_frames = frame_mismatches[event["id"]]
            event["frames"] = reported_frames
            event["audio_duration_seconds"] = round(reported_frames / benchmark.SAMPLE_RATE_HZ, 6)
    missing_wavs = missing_wavs or set()
    corrupt_wavs = corrupt_wavs or set()

    class FakeSampler:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def set_phase(self, _phase):
            pass

        def stop(self):
            return {
                "rss_baseline_kib": 1,
                "pss_baseline_kib": 1,
                "rss_peak_kib": 1,
                "pss_peak_kib": 1,
                "rss_incremental_peak_kib": 0,
                "pss_incremental_peak_kib": 0,
                "phases": {},
            }

    class FakeProcess:
        pid = 123456

        def __init__(self, events):
            self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
            self.stderr = io.StringIO("")

        @staticmethod
        def wait(timeout=None):
            return 0

        @staticmethod
        def poll():
            return 0

    def fake_popen(*_args, **_kwargs):
        for sample_id in set(sample_ids):
            source_path = tmp_path / "sample-source" / f"{sample_id}.wav"
            target_path = output_dir / f"{sample_id}.wav"
            if sample_id in missing_wavs:
                continue
            target_path.write_bytes(source_path.read_bytes())
            if sample_id in corrupt_wavs:
                target_path.write_bytes(b"corrupt wav")
        events = [
            {"event": "model_ready", "model_load_ms": 1.0, "runtime": {}},
            {"event": "phase", "phase": "loaded_idle"},
            *sample_events,
            {"event": "phase", "phase": "shutdown"},
            {
                "event": "worker_result",
                "model_load_ms": 1.0,
                "model_load_cpu_seconds": 0.0,
                "loaded_idle": {"cpu_seconds": 0.0, "cpu_mean_percent": 0.0},
                "runtime": {},
                "synthesized_count": worker_count,
            },
        ]
        return FakeProcess(events)

    monkeypatch.setattr(benchmark, "ProcessSampler", FakeSampler)
    monkeypatch.setattr(benchmark.platform, "platform", lambda: "test-linux-x86_64")
    monkeypatch.setattr(benchmark.subprocess, "Popen", fake_popen)
    return args


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


def test_run_completes_only_when_events_worker_count_and_wavs_match(tmp_path, monkeypatch):
    args = _run_fake_worker(tmp_path, monkeypatch, ["sample_a", "sample_b"], 2)

    metrics = benchmark.run_benchmark(args)

    assert metrics["status"] == "completed"
    assert metrics["result_validation"]["valid"] is True
    assert metrics["result_validation"]["expected_count"] == 2
    assert metrics["result_validation"]["event_count"] == 2
    assert metrics["result_validation"]["worker_synthesized_count"] == 2
    assert [artifact["valid"] for artifact in metrics["result_validation"]["artifacts"]] == [True, True]


def test_reported_frames_matching_reported_duration_but_not_wav_fails_closed(tmp_path, monkeypatch):
    args = _run_fake_worker(
        tmp_path,
        monkeypatch,
        ["sample_a", "sample_b"],
        2,
        frame_mismatches={"sample_a": (8, 4)},
    )

    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark.run_benchmark(args)

    assert raised.value.code == "artifact_invalid"
    assert str(raised.value) == benchmark.ERROR_MESSAGES["artifact_invalid"]
    metrics = json.loads((args.output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["status"] == "failed"
    assert metrics["error_code"] == "artifact_invalid"
    assert metrics["result_validation"]["valid"] is False
    assert "artifact_invalid" in metrics["result_validation"]["issue_codes"]
    assert "sample_a" in metrics["result_validation"]["invalid_wav_ids"]

    artifact = next(item for item in metrics["result_validation"]["artifacts"] if item["id"] == "sample_a")
    sample = next(item for item in metrics["samples"] if item["id"] == "sample_a")
    assert sf.info(args.output_dir / "sample_a.wav").frames == 8
    assert sample["frames"] == 4
    assert sample["audio_duration_seconds"] == round(4 / benchmark.SAMPLE_RATE_HZ, 6)
    assert artifact["actual_size_bytes"] == sample["wav_bytes"]
    assert artifact["reported_size_matches"] is True
    assert artifact["actual_sha256"] == sample["wav_sha256"]
    assert artifact["reported_sha256_matches"] is True
    assert artifact["reported_metadata_matches"] is False


@pytest.mark.parametrize(
    ("sample_ids", "worker_count", "missing_wavs", "corrupt_wavs", "error_code", "issue_code"),
    [
        (["sample_a"], 1, set(), set(), "result_incomplete", "sample_count_mismatch"),
        (["sample_a", "sample_a"], 2, set(), set(), "result_incomplete", "sample_id_duplicate"),
        (["sample_a", "sample_b"], 1, set(), set(), "result_incomplete", "worker_count_mismatch"),
        (["sample_a", "sample_b"], 2, {"sample_b"}, set(), "artifact_invalid", "artifact_file_set_mismatch"),
        (["sample_a", "sample_b"], 2, set(), {"sample_b"}, "artifact_invalid", "artifact_invalid"),
    ],
    ids=["missing-sample", "duplicate-sample", "worker-count-diverges", "wav-missing", "wav-corrupt"],
)
def test_inconsistent_results_fail_and_preserve_metrics(
    tmp_path,
    monkeypatch,
    sample_ids,
    worker_count,
    missing_wavs,
    corrupt_wavs,
    error_code,
    issue_code,
):
    args = _run_fake_worker(
        tmp_path,
        monkeypatch,
        sample_ids,
        worker_count,
        missing_wavs=missing_wavs,
        corrupt_wavs=corrupt_wavs,
    )

    with pytest.raises(benchmark.BenchmarkError) as raised:
        benchmark.run_benchmark(args)

    assert raised.value.code == error_code
    assert str(raised.value) == benchmark.ERROR_MESSAGES[error_code]
    metrics = json.loads((args.output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["status"] == "failed"
    assert metrics["error_code"] == error_code
    assert metrics["result_validation"]["valid"] is False
    assert issue_code in metrics["result_validation"]["issue_codes"]
    assert len(metrics["samples"]) == len(sample_ids)
    assert metrics["process"]["partial_files_remaining"] is False
    if error_code == "artifact_invalid":
        assert "sample_b" in metrics["result_validation"]["invalid_wav_ids"]


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
        "result_validation": {
            "valid": True,
            "error_code": None,
            "expected_count": 1,
            "event_count": 1,
            "worker_synthesized_count": 1,
            "expected_ids": ["short"],
            "observed_ids": ["short"],
            "missing_ids": [],
            "duplicate_ids": [],
            "unexpected_ids": [],
            "missing_wav_ids": [],
            "invalid_wav_ids": [],
            "unexpected_wav_count": 0,
            "artifacts": [],
            "issue_codes": [],
        },
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
