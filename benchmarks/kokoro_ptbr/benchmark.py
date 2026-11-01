#!/usr/bin/env python3
"""Run a pinned, offline Kokoro PT-BR benchmark in an isolated subprocess."""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "model_artifacts.json"
MAX_TEXT_CHARS = 400
MAX_PHONEMES = 510
SAMPLE_PREFIX = ".kokoro-bench-"
SAMPLE_RATE_HZ = 24_000
_CANCEL_REQUESTED = False

ERROR_MESSAGES = {
    "downloads_not_authorized": "Asset downloads require the explicit --allow-downloads flag.",
    "asset_missing": "Pinned model assets are missing. Run fetch-assets explicitly, then retry.",
    "asset_corrupt": "A pinned model asset failed its size or SHA-256 check.",
    "asset_download_failed": "The explicit model asset download failed; no benchmark was started.",
    "model_config_mismatch": "The requested model, revision, voice, or sample rate is not the pinned benchmark candidate.",
    "corpus_missing": "The selected corpus file does not exist or is not readable.",
    "corpus_invalid": "The corpus is invalid; each line must contain one unique ID, category, and nonempty text.",
    "input_too_long": "A corpus item exceeds the benchmark limit of 400 characters.",
    "output_invalid": "The output directory must be empty so this run cannot overwrite existing files.",
    "path_invalid": "A configured directory is invalid or overlaps another benchmark directory.",
    "dependency_unavailable": "A pinned benchmark dependency could not be loaded.",
    "model_load_failed": "The pinned model could not be loaded on CPU.",
    "phonemizer_failed": "Portuguese phonemization failed for a corpus item.",
    "phonemizer_empty": "Portuguese phonemization returned no usable phonemes.",
    "phoneme_limit": "A corpus item exceeds Kokoro's 510-phoneme inference limit.",
    "synthesis_failed": "CPU inference failed for a corpus item.",
    "audio_invalid": "Kokoro returned empty, non-finite, or invalid audio.",
    "audio_write_failed": "The generated WAV could not be written and validated.",
    "result_incomplete": "Worker results do not match the requested corpus; see metrics.json.",
    "artifact_invalid": "A generated WAV is missing or inconsistent; see metrics.json.",
    "worker_protocol_failed": "The benchmark worker returned an invalid event stream.",
    "interrupted": "The benchmark was interrupted; partial files were removed.",
    "benchmark_failed": "The benchmark could not be completed.",
}


class BenchmarkError(Exception):
    def __init__(self, code: str):
        self.code = code if code in ERROR_MESSAGES else "benchmark_failed"
        super().__init__(ERROR_MESSAGES[self.code])


class WorkerCancelled(Exception):
    pass


def _read_spec() -> dict[str, Any]:
    try:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise BenchmarkError("benchmark_failed") from None
    if not isinstance(spec.get("assets"), list) or not spec["assets"]:
        raise BenchmarkError("benchmark_failed")
    return spec


def _valid_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _valid_idle_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be between 0 and 30 seconds") from None
    if not math.isfinite(parsed) or parsed < 0 or parsed > 30:
        raise argparse.ArgumentTypeError("must be between 0 and 30 seconds")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch-assets", help="explicitly fetch the pinned public model files")
    fetch.add_argument("--model-id", required=True)
    fetch.add_argument("--model-revision", required=True)
    fetch.add_argument("--voice", required=True)
    fetch.add_argument("--model-dir", type=Path, required=True)
    fetch.add_argument("--cache-dir", type=Path, required=True)
    fetch.add_argument("--allow-downloads", action="store_true")

    verify = subparsers.add_parser("verify-assets", help="verify local pinned files without network access")
    verify.add_argument("--model-id", required=True)
    verify.add_argument("--model-revision", required=True)
    verify.add_argument("--voice", required=True)
    verify.add_argument("--model-dir", type=Path, required=True)

    run = subparsers.add_parser("run", help="synthesize the selected corpus on CPU")
    run.add_argument("--model-id", required=True)
    run.add_argument("--model-revision", required=True)
    run.add_argument("--voice", required=True)
    run.add_argument("--model-dir", type=Path, required=True)
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--cache-dir", type=Path, required=True, help="explicit Hugging Face download cache to measure")
    run.add_argument("--uv-cache-dir", type=Path, required=True, help="explicit uv cache directory to measure")
    run.add_argument("--limit", type=_valid_positive_int, help="short diagnostic: synthesize only the first N corpus items")
    run.add_argument("--idle-seconds", type=_valid_idle_seconds, default=2.0)
    run.add_argument("--threads", type=_valid_positive_int, default=4)

    # Private child-process entry point. It is deliberately not documented as a user command.
    worker = subparsers.add_parser("_worker")
    worker.add_argument("--model-id", required=True)
    worker.add_argument("--model-revision", required=True)
    worker.add_argument("--voice", required=True)
    worker.add_argument("--model-dir", type=Path, required=True)
    worker.add_argument("--corpus", type=Path, required=True)
    worker.add_argument("--output-dir", type=Path, required=True)
    worker.add_argument("--idle-seconds", type=_valid_idle_seconds, required=True)
    worker.add_argument("--threads", type=_valid_positive_int, required=True)
    worker.add_argument("--limit", type=_valid_positive_int)
    return parser


def _validate_candidate(args: argparse.Namespace) -> None:
    spec = _read_spec()
    if (
        args.model_id != spec["model_id"]
        or args.model_revision != spec["model_revision"]
        or args.voice != spec["voice"]
        or spec["sample_rate_hz"] != SAMPLE_RATE_HZ
    ):
        raise BenchmarkError("model_config_mismatch")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_assets(model_dir: Path) -> dict[str, Path]:
    spec = _read_spec()
    paths: dict[str, Path] = {}
    for asset in spec["assets"]:
        path = model_dir / asset["path"]
        if not path.is_file():
            raise BenchmarkError("asset_missing")
        try:
            if path.stat().st_size != asset["size_bytes"] or _sha256(path) != asset["sha256"]:
                raise BenchmarkError("asset_corrupt")
        except OSError:
            raise BenchmarkError("asset_corrupt") from None
        paths[asset["path"]] = path
    return paths


def _atomic_copy(source: Path, target: Path, expected: dict[str, Any]) -> None:
    if target.exists():
        if target.is_file() and target.stat().st_size == expected["size_bytes"] and _sha256(target) == expected["sha256"]:
            return
        raise BenchmarkError("asset_corrupt")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f"{SAMPLE_PREFIX}asset-", suffix=".partial", dir=target.parent, delete=False) as temp:
            temp_path = Path(temp.name)
        shutil.copyfile(source, temp_path)
        if temp_path.stat().st_size != expected["size_bytes"] or _sha256(temp_path) != expected["sha256"]:
            raise BenchmarkError("asset_corrupt")
        os.replace(temp_path, target)
        temp_path = None
    except BenchmarkError:
        raise
    except Exception:
        raise BenchmarkError("asset_download_failed") from None
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def fetch_assets(args: argparse.Namespace) -> None:
    if not args.allow_downloads:
        raise BenchmarkError("downloads_not_authorized")
    _validate_candidate(args)
    spec = _read_spec()
    args.model_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    # The Hub uses a separate Xet cache/log root in addition to cache_dir.
    # Keep both inside the explicitly supplied benchmark cache.
    os.environ["HF_HOME"] = str(args.cache_dir / "hf-home")
    os.environ["HF_HUB_CACHE"] = str(args.cache_dir)
    os.environ["HF_XET_CACHE"] = str(args.cache_dir / "xet")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        from huggingface_hub import hf_hub_download
    except Exception:
        raise BenchmarkError("dependency_unavailable") from None
    try:
        for asset in spec["assets"]:
            target = args.model_dir / asset["path"]
            if target.exists():
                if not target.is_file() or target.stat().st_size != asset["size_bytes"] or _sha256(target) != asset["sha256"]:
                    raise BenchmarkError("asset_corrupt")
                continue
            cached_file = Path(
                hf_hub_download(
                    repo_id=args.model_id,
                    filename=asset["path"],
                    revision=args.model_revision,
                    cache_dir=str(args.cache_dir),
                    token=False,
                    local_files_only=False,
                )
            )
            _atomic_copy(cached_file, target, asset)
        paths = verify_assets(args.model_dir)
        manifest = {
            "model_id": spec["model_id"],
            "model_revision": spec["model_revision"],
            "voice": spec["voice"],
            "license": spec["license"],
            "assets": [
                {"path": asset["path"], "size_bytes": paths[asset["path"]].stat().st_size, "sha256": _sha256(paths[asset["path"]])}
                for asset in spec["assets"]
            ],
        }
        manifest_path = args.model_dir / "asset-manifest.json"
        encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if manifest_path.exists():
            if manifest_path.read_bytes() != encoded:
                raise BenchmarkError("asset_corrupt")
        else:
            temp_path = manifest_path.with_name(f"{SAMPLE_PREFIX}manifest.partial")
            try:
                temp_path.write_bytes(encoded)
                os.replace(temp_path, manifest_path)
            finally:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
    except BenchmarkError:
        raise
    except Exception:
        raise BenchmarkError("asset_download_failed") from None


def load_corpus(path: Path, limit: int | None = None) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise BenchmarkError("corpus_missing") from None
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        for line in lines:
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict) or set(item) != {"id", "category", "text"}:
                raise ValueError
            item_id = item["id"]
            category = item["category"]
            text = item["text"]
            if (
                not isinstance(item_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", item_id)
                or item_id in seen
                or not isinstance(category, str)
                or not category.strip()
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ValueError
            if len(text) > MAX_TEXT_CHARS:
                raise BenchmarkError("input_too_long")
            seen.add(item_id)
            items.append({"id": item_id, "category": category.strip(), "text": text.strip()})
    except BenchmarkError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError):
        raise BenchmarkError("corpus_invalid") from None
    if not items:
        raise BenchmarkError("corpus_invalid")
    return items[:limit] if limit is not None else items


def _directory_apparent_bytes(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            candidate = Path(current) / name
            with contextlib.suppress(OSError):
                total += candidate.stat().st_size
    return total


def _prepare_output_dir(output_dir: Path, model_dir: Path, cache_dir: Path, uv_cache_dir: Path) -> None:
    try:
        resolved_output = output_dir.resolve()
        protected = [model_dir.resolve(), cache_dir.resolve(), uv_cache_dir.resolve()]
        if any(resolved_output == other or resolved_output.is_relative_to(other) or other.is_relative_to(resolved_output) for other in protected):
            raise BenchmarkError("path_invalid")
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.iterdir()):
            raise BenchmarkError("output_invalid")
    except BenchmarkError:
        raise
    except OSError:
        raise BenchmarkError("path_invalid") from None


def worker_environment(threads: int) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        env.pop(key, None)
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "OMP_NUM_THREADS": str(threads),
            "MKL_NUM_THREADS": str(threads),
            "OPENBLAS_NUM_THREADS": str(threads),
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    return env


def _read_proc_snapshot(pid: int) -> dict[str, float] | None:
    proc_dir = Path("/proc") / str(pid)
    try:
        stat_text = (proc_dir / "stat").read_text(encoding="ascii")
        fields = stat_text[stat_text.rfind(")") + 2 :].split()
        cpu_ticks = int(fields[11]) + int(fields[12])
        status = (proc_dir / "status").read_text(encoding="ascii")
        rss_kib = 0.0
        hwm_kib = 0.0
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss_kib = float(line.split()[1])
            elif line.startswith("VmHWM:"):
                hwm_kib = float(line.split()[1])
        pss_kib: float | None = None
        try:
            for line in (proc_dir / "smaps_rollup").read_text(encoding="ascii").splitlines():
                if line.startswith("Pss:"):
                    pss_kib = float(line.split()[1])
                    break
        except OSError:
            pss_kib = None
        return {
            "at": time.monotonic(),
            "cpu_seconds": cpu_ticks / os.sysconf("SC_CLK_TCK"),
            "rss_kib": rss_kib,
            "hwm_kib": hwm_kib,
            "pss_kib": pss_kib if pss_kib is not None else -1.0,
        }
    except (OSError, IndexError, ValueError):
        return None


class ProcessSampler:
    def __init__(self, pid: int, interval_seconds: float = 0.1):
        self.pid = pid
        self.interval_seconds = interval_seconds
        self.phase = "startup"
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.baseline: dict[str, float] | None = None
        self.previous: dict[str, float] | None = None
        self.global_peak = {"rss_kib": 0.0, "pss_kib": 0.0, "hwm_kib": 0.0}
        self.phases: dict[str, dict[str, float]] = {}

    def _observe(self) -> None:
        current = _read_proc_snapshot(self.pid)
        if current is None:
            return
        with self.lock:
            if self.baseline is None:
                self.baseline = current
            if self.previous is not None:
                elapsed = max(0.0, current["at"] - self.previous["at"])
                cpu = max(0.0, current["cpu_seconds"] - self.previous["cpu_seconds"])
                phase_stats = self.phases.setdefault(
                    self.phase,
                    {"wall_seconds": 0.0, "cpu_seconds": 0.0, "rss_peak_kib": 0.0, "pss_peak_kib": 0.0, "cpu_peak_percent": 0.0},
                )
                phase_stats["wall_seconds"] += elapsed
                phase_stats["cpu_seconds"] += cpu
                if elapsed > 0:
                    phase_stats["cpu_peak_percent"] = max(phase_stats["cpu_peak_percent"], cpu / elapsed * 100.0)
                phase_stats["rss_peak_kib"] = max(phase_stats["rss_peak_kib"], current["rss_kib"])
                if current["pss_kib"] >= 0:
                    phase_stats["pss_peak_kib"] = max(phase_stats["pss_peak_kib"], current["pss_kib"])
            self.global_peak["rss_kib"] = max(self.global_peak["rss_kib"], current["rss_kib"])
            self.global_peak["hwm_kib"] = max(self.global_peak["hwm_kib"], current["hwm_kib"])
            if current["pss_kib"] >= 0:
                self.global_peak["pss_kib"] = max(self.global_peak["pss_kib"], current["pss_kib"])
            self.previous = current

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self._observe()
            self.stop_event.wait(self.interval_seconds)
        self._observe()

    def start(self) -> None:
        self._observe()
        self.thread = threading.Thread(target=self._loop, name="kokoro-proc-sampler", daemon=True)
        self.thread.start()

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        with self.lock:
            baseline = self.baseline or {"rss_kib": 0.0, "pss_kib": -1.0}
            phases: dict[str, dict[str, float | None]] = {}
            for name, stat in self.phases.items():
                wall = stat["wall_seconds"]
                phases[name] = {
                    **stat,
                    "cpu_mean_percent": stat["cpu_seconds"] / wall * 100.0 if wall > 0 else 0.0,
                }
            return {
                "sampling_interval_ms": round(self.interval_seconds * 1000, 3),
                "rss_baseline_kib": baseline["rss_kib"],
                "pss_baseline_kib": baseline["pss_kib"] if baseline["pss_kib"] >= 0 else None,
                "rss_peak_kib": self.global_peak["rss_kib"],
                "pss_peak_kib": self.global_peak["pss_kib"] or None,
                "rss_incremental_peak_kib": max(0.0, self.global_peak["rss_kib"] - baseline["rss_kib"]),
                "pss_incremental_peak_kib": (
                    max(0.0, self.global_peak["pss_kib"] - baseline["pss_kib"])
                    if baseline["pss_kib"] >= 0 and self.global_peak["pss_kib"]
                    else None
                ),
                "phases": phases,
            }


def _drain_stderr(stream: Any) -> None:
    if stream is None:
        return
    with contextlib.suppress(Exception):
        for _line in stream:
            pass


def _terminate_process(process: subprocess.Popen[str], grace_seconds: float = 5.0) -> bool:
    if process.poll() is not None:
        return True
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return True
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)
        return process.poll() is not None


def cleanup_partial_outputs(output_dir: Path) -> int:
    removed = 0
    if not output_dir.exists() or not output_dir.is_dir():
        return removed
    for path in output_dir.glob(f"{SAMPLE_PREFIX}*.partial*"):
        if path.is_file() or path.is_symlink():
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
    return removed


def _audio_device_probe(device_dir: Path = Path("/dev/snd")) -> dict[str, Any]:
    devices = device_dir
    try:
        present = devices.is_dir() and any(devices.iterdir())
    except OSError:
        present = False
    players = [name for name in ("paplay", "pw-play", "aplay") if shutil.which(name)]
    return {
        "device_nodes_detected": bool(present),
        "playback_commands_detected": players,
        "playback_attempted_by_benchmark": False,
        "synthesis_requires_audio_device": False,
        "human_listening": "UNVERIFIED",
    }


def _mean_cpu_percent(cpu_seconds: float, wall_seconds: float) -> float:
    return round(cpu_seconds / wall_seconds * 100.0, 3) if wall_seconds > 0 else 0.0


def _inspect_result_wav(path: Path, event: dict[str, Any] | None, sample_id: str) -> dict[str, Any]:
    filename = f"{sample_id}.wav"
    exists = path.is_file() and not path.is_symlink()
    actual_size: int | None = None
    actual_hash: str | None = None
    actual_frames: int | None = None
    actual_sample_rate: int | None = None
    header_valid = False
    event_valid = event is not None
    size_matches = False
    hash_matches = False
    reported_file_matches = False

    if exists:
        try:
            actual_size = path.stat().st_size
            actual_hash = _sha256(path)
            with wave.open(str(path), "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                sample_rate = wav.getframerate()
                actual_sample_rate = sample_rate
                actual_frames = wav.getnframes()
                header_valid = (
                    wav.getcomptype() == "NONE"
                    and channels == 1
                    and sample_width == 2
                    and sample_rate == SAMPLE_RATE_HZ
                    and actual_frames > 0
                )
                remaining_frames = actual_frames
                while header_valid and remaining_frames > 0:
                    chunk = wav.readframes(min(8192, remaining_frames))
                    frame_bytes = channels * sample_width
                    if not chunk or len(chunk) % frame_bytes != 0:
                        header_valid = False
                        break
                    remaining_frames -= len(chunk) // frame_bytes
                header_valid = header_valid and remaining_frames == 0
        except (EOFError, OSError, ValueError, wave.Error):
            header_valid = False

    if event is not None:
        reported_file_matches = event.get("audio_file") == filename
        reported_size = event.get("wav_bytes")
        size_matches = (
            isinstance(reported_size, int)
            and not isinstance(reported_size, bool)
            and actual_size == reported_size
        )
        reported_hash = event.get("wav_sha256")
        hash_matches = (
            isinstance(reported_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", reported_hash) is not None
            and actual_hash == reported_hash
        )
        try:
            reported_frames = event.get("frames")
            reported_rate = event.get("sample_rate_hz")
            reported_channels = event.get("channels")
            reported_duration = float(event.get("audio_duration_seconds"))
            metadata_matches = (
                isinstance(reported_frames, int)
                and not isinstance(reported_frames, bool)
                and isinstance(reported_rate, int)
                and not isinstance(reported_rate, bool)
                and isinstance(reported_channels, int)
                and not isinstance(reported_channels, bool)
                and reported_rate == SAMPLE_RATE_HZ
                and reported_channels == 1
                and event.get("subtype") == "PCM_16"
                and math.isfinite(reported_duration)
                and actual_frames is not None
                and reported_frames == actual_frames
                and actual_sample_rate is not None
                and actual_sample_rate > 0
                and round(actual_frames / actual_sample_rate, 6) == round(reported_duration, 6)
            )
        except (TypeError, ValueError, OverflowError):
            metadata_matches = False
    else:
        metadata_matches = False

    valid = exists and event_valid and reported_file_matches and size_matches and hash_matches and header_valid and metadata_matches
    return {
        "id": sample_id,
        "file": filename,
        "exists": exists,
        "event_present": event_valid,
        "reported_file_matches": reported_file_matches,
        "actual_size_bytes": actual_size,
        "reported_size_matches": size_matches,
        "actual_sha256": actual_hash,
        "reported_sha256_matches": hash_matches,
        "wav_header_and_data_valid": header_valid,
        "reported_metadata_matches": metadata_matches,
        "valid": valid,
    }


def validate_synthesis_result(
    corpus: list[dict[str, str]],
    sample_events: list[dict[str, Any]],
    worker_result: dict[str, Any] | None,
    output_dir: Path,
) -> dict[str, Any]:
    expected_ids = [item["id"] for item in corpus]
    expected_id_set = set(expected_ids)
    observed_ids: list[str | None] = []
    for event in sample_events:
        sample_id = event.get("id")
        if isinstance(sample_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", sample_id):
            observed_ids.append(sample_id)
        else:
            observed_ids.append(None)

    observed_valid_ids = [sample_id for sample_id in observed_ids if sample_id is not None]
    id_counts: dict[str, int] = {}
    for sample_id in observed_valid_ids:
        id_counts[sample_id] = id_counts.get(sample_id, 0) + 1
    duplicate_ids = sorted(sample_id for sample_id, count in id_counts.items() if count > 1)
    missing_ids = [sample_id for sample_id in expected_ids if id_counts.get(sample_id, 0) == 0]
    unexpected_ids = sorted(set(observed_valid_ids) - expected_id_set)

    issue_codes: list[str] = []

    def add_issue(code: str) -> None:
        if code not in issue_codes:
            issue_codes.append(code)

    if len(sample_events) != len(corpus):
        add_issue("sample_count_mismatch")
    if any(sample_id is None for sample_id in observed_ids):
        add_issue("sample_id_invalid")
    if duplicate_ids:
        add_issue("sample_id_duplicate")
    if missing_ids or unexpected_ids:
        add_issue("sample_id_mismatch")

    worker_count = worker_result.get("synthesized_count") if worker_result else None
    worker_count_valid = (
        isinstance(worker_count, int)
        and not isinstance(worker_count, bool)
        and worker_count == len(corpus)
        and worker_count == len(sample_events)
    )
    if not worker_count_valid:
        add_issue("worker_count_mismatch")

    expected_wav_names = {f"{sample_id}.wav" for sample_id in expected_ids}
    actual_wav_paths = list(output_dir.glob("*.wav"))
    actual_wav_names = {path.name for path in actual_wav_paths}
    missing_file_ids = [sample_id for sample_id in expected_ids if f"{sample_id}.wav" not in actual_wav_names]
    unexpected_wav_count = len(actual_wav_names - expected_wav_names)
    if missing_file_ids or unexpected_wav_count or len(actual_wav_paths) != len(expected_ids):
        add_issue("artifact_file_set_mismatch")

    event_by_id: dict[str, list[dict[str, Any]]] = {}
    for event, sample_id in zip(sample_events, observed_ids, strict=True):
        if sample_id is not None and sample_id in expected_id_set:
            event_by_id.setdefault(sample_id, []).append(event)

    artifacts: list[dict[str, Any]] = []
    invalid_file_ids: list[str] = []
    for sample_id in expected_ids:
        matching_events = event_by_id.get(sample_id, [])
        event = matching_events[0] if len(matching_events) == 1 else None
        artifact = _inspect_result_wav(output_dir / f"{sample_id}.wav", event, sample_id)
        artifact["event_count"] = len(matching_events)
        if len(matching_events) != 1:
            artifact["valid"] = False
        if not artifact["valid"]:
            invalid_file_ids.append(sample_id)
        artifacts.append(artifact)
    if invalid_file_ids or unexpected_wav_count:
        add_issue("artifact_invalid")

    structural_issues = {
        "sample_count_mismatch",
        "sample_id_invalid",
        "sample_id_duplicate",
        "sample_id_mismatch",
        "worker_count_mismatch",
    }
    if any(code in structural_issues for code in issue_codes):
        error_code = "result_incomplete"
    elif "artifact_file_set_mismatch" in issue_codes or "artifact_invalid" in issue_codes:
        error_code = "artifact_invalid"
    else:
        error_code = None

    return {
        "valid": not issue_codes,
        "error_code": error_code,
        "expected_count": len(corpus),
        "event_count": len(sample_events),
        "worker_synthesized_count": worker_count if isinstance(worker_count, int) and not isinstance(worker_count, bool) else None,
        "expected_ids": expected_ids,
        "observed_ids": observed_ids,
        "missing_ids": missing_ids,
        "duplicate_ids": duplicate_ids,
        "unexpected_ids": unexpected_ids,
        "missing_wav_ids": missing_file_ids,
        "invalid_wav_ids": invalid_file_ids,
        "unexpected_wav_count": unexpected_wav_count,
        "artifacts": artifacts,
        "issue_codes": issue_codes,
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    _validate_candidate(args)
    asset_paths = verify_assets(args.model_dir)
    corpus = load_corpus(args.corpus, args.limit)
    _prepare_output_dir(args.output_dir, args.model_dir, args.cache_dir, args.uv_cache_dir)

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--model-id",
        args.model_id,
        "--model-revision",
        args.model_revision,
        "--voice",
        args.voice,
        "--model-dir",
        str(args.model_dir.resolve()),
        "--corpus",
        str(args.corpus.resolve()),
        "--output-dir",
        str(args.output_dir.resolve()),
        "--idle-seconds",
        str(args.idle_seconds),
        "--threads",
        str(args.threads),
    ]
    if args.limit is not None:
        command.extend(("--limit", str(args.limit)))

    run_started_utc = datetime.now(timezone.utc).isoformat()
    launched_ns = time.monotonic_ns()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(HERE),
            env=worker_environment(args.threads),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError:
        cleanup_partial_outputs(args.output_dir)
        raise BenchmarkError("dependency_unavailable") from None

    sampler = ProcessSampler(process.pid)
    sampler.start()
    stderr_thread = threading.Thread(target=_drain_stderr, args=(process.stderr,), daemon=True)
    stderr_thread.start()
    events: list[dict[str, Any]] = []
    protocol_failed = False
    interrupted = False
    process_exited_ns: int | None = None
    try:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                    raise ValueError
                event["parent_received_ns"] = time.monotonic_ns()
                events.append(event)
                if event["event"] == "phase":
                    phase = event.get("phase")
                    if not isinstance(phase, str):
                        raise ValueError
                    sampler.set_phase(phase)
            except (json.JSONDecodeError, ValueError, TypeError):
                protocol_failed = True
                _terminate_process(process)
                break
    except KeyboardInterrupt:
        interrupted = True
        _terminate_process(process)
    finally:
        with contextlib.suppress(Exception):
            process.wait(timeout=2.0)
        if process.poll() is not None:
            process_exited_ns = time.monotonic_ns()
        resources = sampler.stop()
        stderr_thread.join(timeout=1.0)
        cleanup_partial_outputs(args.output_dir)

    finished_ns = process_exited_ns or time.monotonic_ns()
    exit_code = process.poll()
    model_ready_event = next((item for item in events if item.get("event") == "model_ready"), None)
    sample_events = [item for item in events if item.get("event") == "sample_complete"]
    worker_result = next((item for item in events if item.get("event") == "worker_result"), None)
    worker_error = next((item for item in events if item.get("event") == "worker_error"), None)
    shutdown_event = next((item for item in events if item.get("event") == "phase" and item.get("phase") == "shutdown"), None)
    result_validation = validate_synthesis_result(corpus, sample_events, worker_result, args.output_dir)
    if interrupted:
        status = "interrupted"
        error_code = "interrupted"
    elif protocol_failed:
        status = "failed"
        error_code = "worker_protocol_failed"
    elif worker_error or exit_code not in (0, None) or worker_result is None:
        status = "failed"
        error_code = (worker_error or {}).get("code", "benchmark_failed")
        if error_code not in ERROR_MESSAGES:
            error_code = "benchmark_failed"
    elif not result_validation["valid"]:
        status = "failed"
        error_code = result_validation["error_code"] or "benchmark_failed"
    else:
        status = "completed"
        error_code = None

    sample_duration = sum(float(item.get("audio_duration_seconds", 0.0)) for item in sample_events)
    total_generation_seconds = sum(float(item.get("generation_ms", 0.0)) for item in sample_events) / 1000.0
    first_sample = sample_events[0] if sample_events else None
    shutdown_started_ns = shutdown_event.get("parent_received_ns") if shutdown_event else None
    shutdown_ms = None
    if isinstance(shutdown_started_ns, int):
        try:
            shutdown_ms = round(max(0, finished_ns - shutdown_started_ns) / 1_000_000, 3)
        except TypeError:
            shutdown_ms = None

    model_assets_bytes = sum(asset_paths[name].stat().st_size for name in asset_paths)
    output_wavs = sorted(args.output_dir.glob("*.wav"))
    output_wavs_bytes = sum(path.stat().st_size for path in output_wavs)
    spec = _read_spec()
    metrics = {
        "schema_version": 1,
        "status": status,
        "error_code": error_code,
        "run_started_utc": run_started_utc,
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "distribution": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "candidate": {
            "model_id": args.model_id,
            "model_revision": args.model_revision,
            "model_release": spec["model_release"],
            "voice": args.voice,
            "license": spec["license"],
            "inference_device": "cpu",
            "torch_threads": args.threads,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "assets": [
                {"path": asset["path"], "size_bytes": asset["size_bytes"], "sha256": asset["sha256"]}
                for asset in spec["assets"]
            ],
        },
        "timing": {
            "process_spawn_to_model_ready_ms": round((model_ready_event["parent_received_ns"] - launched_ns) / 1_000_000, 3)
            if model_ready_event and isinstance(model_ready_event.get("parent_received_ns"), int)
            else None,
            "model_load_ms": worker_result.get("model_load_ms") if worker_result else None,
            "model_load_cpu_seconds": worker_result.get("model_load_cpu_seconds") if worker_result else None,
            "loaded_idle_cpu_seconds": (worker_result.get("loaded_idle") or {}).get("cpu_seconds") if worker_result else None,
            "loaded_idle_cpu_mean_percent": (worker_result.get("loaded_idle") or {}).get("cpu_mean_percent") if worker_result else None,
            "first_inference_after_model_load_ms": first_sample.get("generation_ms") if first_sample else None,
            "process_spawn_to_first_wav_ready_ms": round((first_sample["parent_received_ns"] - launched_ns) / 1_000_000, 3)
            if first_sample and isinstance(first_sample.get("parent_received_ns"), int)
            else None,
            "warm_generation_median_ms": round(
                sorted(float(item["generation_ms"]) for item in sample_events[1:])[len(sample_events[1:]) // 2], 3
            )
            if len(sample_events) > 1
            else None,
            "audio_duration_seconds_total": round(sample_duration, 6),
            "generation_seconds_total": round(total_generation_seconds, 6),
            "synthesis_cpu_seconds_total": round(sum(float(item.get("cpu_seconds", 0.0)) for item in sample_events), 6),
            "synthesis_cpu_mean_percent": round(
                sum(float(item.get("cpu_seconds", 0.0)) for item in sample_events) / total_generation_seconds * 100.0, 3
            )
            if total_generation_seconds > 0
            else None,
            "real_time_factor": round(total_generation_seconds / sample_duration, 6) if sample_duration > 0 else None,
            "audio_throughput_x_realtime": round(sample_duration / total_generation_seconds, 6)
            if total_generation_seconds > 0
            else None,
            "loaded_idle_seconds_requested": args.idle_seconds,
            "shutdown_phase_to_process_exit_ms": shutdown_ms,
            "process_spawn_to_exit_ms": round((finished_ns - launched_ns) / 1_000_000, 3),
        },
        "resources": resources,
        "disk": {
            "model_assets_apparent_bytes": model_assets_bytes,
            "huggingface_cache_apparent_bytes": _directory_apparent_bytes(args.cache_dir),
            "uv_cache_apparent_bytes": _directory_apparent_bytes(args.uv_cache_dir),
            "installed_benchmark_environment_apparent_bytes": _directory_apparent_bytes(Path(sys.prefix)),
            "generated_wav_apparent_bytes": output_wavs_bytes,
            "size_method": "sum of apparent file lengths; use du -s for allocated blocks",
        },
        "audio_output": _audio_device_probe(),
        "samples": [
            {key: value for key, value in item.items() if key not in {"event", "received_ns", "parent_received_ns", "at_ns"}}
            for item in sample_events
        ],
        "result_validation": result_validation,
        "process": {
            "worker_exit_code": exit_code,
            "worker_process_exited": exit_code is not None,
            "persistent_service_started": False,
            "persistent_process_after_exit": False,
            "partial_files_remaining": any(args.output_dir.glob(f"{SAMPLE_PREFIX}*.partial*")),
        },
        "method": {
            "download_during_run": False,
            "network_disabled_for_worker": True,
            "audio_playback_performed": False,
            "corpus_file": args.corpus.name,
            "corpus_items_requested": len(corpus),
            "corpus_items_synthesized": len(sample_events),
            "metric_timing_source": "parent monotonic clock and Linux /proc sampler; 100 ms interval",
        },
    }
    if worker_result:
        metrics["runtime"] = worker_result.get("runtime")
    if worker_error:
        metrics["worker_error_code"] = worker_error.get("code")
    validate_metrics_schema(metrics)
    metrics_path = args.output_dir / "metrics.json"
    _atomic_json_write(metrics_path, metrics)
    if status != "completed":
        raise BenchmarkError(error_code or "benchmark_failed")
    return metrics


def _atomic_json_write(path: Path, payload: Any) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=f"{SAMPLE_PREFIX}metrics-", suffix=".partial", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except Exception:
        raise BenchmarkError("benchmark_failed") from None
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def validate_metrics_schema(metrics: Any) -> None:
    required = {"schema_version", "status", "error_code", "host", "candidate", "timing", "resources", "disk", "audio_output", "samples", "process", "method", "result_validation"}
    if not isinstance(metrics, dict) or not required.issubset(metrics):
        raise BenchmarkError("worker_protocol_failed")
    if metrics["schema_version"] != 1 or metrics["status"] not in {"completed", "failed", "interrupted"}:
        raise BenchmarkError("worker_protocol_failed")
    for section, keys in {
        "candidate": {"model_id", "model_revision", "voice", "inference_device", "torch_threads", "sample_rate_hz", "assets"},
        "timing": {"process_spawn_to_model_ready_ms", "model_load_ms", "model_load_cpu_seconds", "loaded_idle_cpu_seconds", "loaded_idle_cpu_mean_percent", "first_inference_after_model_load_ms", "process_spawn_to_first_wav_ready_ms", "real_time_factor", "synthesis_cpu_seconds_total", "synthesis_cpu_mean_percent", "shutdown_phase_to_process_exit_ms"},
        "resources": {"rss_baseline_kib", "rss_peak_kib", "rss_incremental_peak_kib", "pss_baseline_kib", "pss_peak_kib", "pss_incremental_peak_kib", "phases"},
        "disk": {"model_assets_apparent_bytes", "huggingface_cache_apparent_bytes", "uv_cache_apparent_bytes", "installed_benchmark_environment_apparent_bytes", "generated_wav_apparent_bytes"},
    }.items():
        if not isinstance(metrics.get(section), dict) or not keys.issubset(metrics[section]):
            raise BenchmarkError("worker_protocol_failed")
    if not isinstance(metrics["samples"], list):
        raise BenchmarkError("worker_protocol_failed")
    if not isinstance(metrics["result_validation"], dict) or not {
        "valid", "error_code", "expected_count", "event_count", "worker_synthesized_count",
        "expected_ids", "observed_ids", "missing_ids", "duplicate_ids", "unexpected_ids",
        "missing_wav_ids", "invalid_wav_ids", "unexpected_wav_count", "artifacts", "issue_codes",
    }.issubset(metrics["result_validation"]):
        raise BenchmarkError("worker_protocol_failed")
    for sample in metrics["samples"]:
        if not isinstance(sample, dict) or not {"id", "audio_file", "audio_duration_seconds", "generation_ms", "cpu_seconds", "cpu_mean_percent", "real_time_factor", "wav_sha256"}.issubset(sample):
            raise BenchmarkError("worker_protocol_failed")


def _worker_emit(payload: dict[str, Any]) -> None:
    if "at_ns" not in payload:
        payload["at_ns"] = time.monotonic_ns()
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    raise WorkerCancelled


def _atomic_write_wav(path: Path, audio: Any, soundfile_module: Any, numpy_module: Any, sample_rate: int) -> dict[str, Any]:
    array = numpy_module.asarray(audio)
    if array.ndim != 1 or array.size == 0 or not numpy_module.isfinite(array).all():
        raise BenchmarkError("audio_invalid")
    array = array.astype(numpy_module.float32, copy=False)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f"{SAMPLE_PREFIX}{path.stem}-", suffix=".partial.wav", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
        soundfile_module.write(str(temp_path), array, sample_rate, format="WAV", subtype="PCM_16")
        info = soundfile_module.info(str(temp_path))
        if info.frames < 1 or info.samplerate != sample_rate or info.channels != 1 or info.format != "WAV":
            raise BenchmarkError("audio_write_failed")
        os.replace(temp_path, path)
        temp_path = None
        clipped = float(numpy_module.mean(numpy_module.abs(array) >= 1.0))
        return {
            "audio_duration_seconds": info.frames / sample_rate,
            "frames": int(info.frames),
            "sample_rate_hz": int(info.samplerate),
            "channels": int(info.channels),
            "subtype": info.subtype,
            "peak_amplitude": float(numpy_module.max(numpy_module.abs(array))),
            "rms_amplitude": float(numpy_module.sqrt(numpy_module.mean(numpy_module.square(array)))),
            "clipped_sample_fraction": clipped,
            "wav_bytes": path.stat().st_size,
            "wav_sha256": _sha256(path),
            "audio_file": path.name,
        }
    except BenchmarkError:
        raise
    except Exception:
        raise BenchmarkError("audio_write_failed") from None
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _build_local_pipeline(
    model_id: str,
    voice_name: str,
    paths: dict[str, Path],
    torch_module: Any,
    model_class: Any,
    pipeline_class: Any,
) -> tuple[Any, Any, Any]:
    model = model_class(
        repo_id=model_id,
        config=str(paths["config.json"]),
        model=str(paths["kokoro-v1_0.pth"]),
    ).to(torch_module.device("cpu")).eval()
    pipeline = pipeline_class(lang_code="p", repo_id=model_id, model=model, device="cpu")
    voice = pipeline.load_voice(str(paths[f"voices/{voice_name}.pt"]))
    return model, pipeline, voice


def _synthesize_text(
    pipeline: Any,
    voice: Any,
    text: str,
    on_phase: Any | None = None,
    ) -> tuple[str, Any, float, float]:
    if on_phase is not None:
        on_phase("phonemization")
    phonemize_start = time.monotonic_ns()
    try:
        phonemes, _tokens = pipeline.g2p(text)
    except Exception:
        raise BenchmarkError("phonemizer_failed") from None
    phonemize_ms = (time.monotonic_ns() - phonemize_start) / 1_000_000
    if not isinstance(phonemes, str) or not phonemes.strip():
        raise BenchmarkError("phonemizer_empty")
    if len(phonemes) > MAX_PHONEMES:
        raise BenchmarkError("phoneme_limit")

    if on_phase is not None:
        on_phase("inference")
    inference_start = time.monotonic_ns()
    try:
        generated = next(pipeline.generate_from_tokens(phonemes, voice=voice))
        audio = generated.audio
    except StopIteration:
        raise BenchmarkError("audio_invalid") from None
    except Exception:
        raise BenchmarkError("synthesis_failed") from None
    inference_ms = (time.monotonic_ns() - inference_start) / 1_000_000
    if audio is None:
        raise BenchmarkError("audio_invalid")
    return phonemes, audio, phonemize_ms, inference_ms


def _worker_main(args: argparse.Namespace) -> int:
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = False
    signal.signal(signal.SIGTERM, _handle_sigterm)
    error_code: str | None = None
    stage = "dependency"
    started_ns = time.monotonic_ns()
    model = pipeline = voice_tensor = None
    worker_result: dict[str, Any] = {}
    try:
        _validate_candidate(args)
        paths = verify_assets(args.model_dir)
        items = load_corpus(args.corpus, args.limit)
        _worker_emit({"event": "phase", "phase": "dependency_load"})
        try:
            import numpy as np
            import soundfile as sf
            import torch
            from kokoro import KPipeline
            from kokoro.model import KModel
            from loguru import logger
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
        except Exception:
            raise BenchmarkError("dependency_unavailable") from None
        with contextlib.suppress(Exception):
            logger.remove()
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        torch.set_grad_enabled(False)
        stage = "model_load"
        _worker_emit({"event": "phase", "phase": "model_load"})
        model_load_started_ns = time.monotonic_ns()
        model_load_started_cpu = time.process_time()
        model, pipeline, voice_tensor = _build_local_pipeline(
            args.model_id, args.voice, paths, torch, KModel, KPipeline
        )
        model_load_ms = (time.monotonic_ns() - model_load_started_ns) / 1_000_000
        model_load_cpu_seconds = time.process_time() - model_load_started_cpu
        parameter_count = sum(parameter.numel() for parameter in model.parameters())
        runtime = {
            "python": platform.python_version(),
            "kokoro": importlib.metadata.version("kokoro"),
            "misaki": importlib.metadata.version("misaki"),
            "torch": torch.__version__,
            "numpy": np.__version__,
            "soundfile": sf.__version__,
            "phonemizer_fork": importlib.metadata.version("phonemizer-fork"),
            "espeakng_loader": importlib.metadata.version("espeakng-loader"),
            "espeak_library_version": str(EspeakWrapper().version),
            "g2p_class": type(pipeline.g2p).__name__,
            "g2p_language": "pt-br",
            "torch_cuda_build": torch.version.cuda,
            "torch_cuda_available": bool(torch.cuda.is_available()),
            "model_parameter_count": parameter_count,
        }
        model_ready_ns = time.monotonic_ns()
        _worker_emit({
            "event": "model_ready",
            "model_load_ms": round(model_load_ms, 3),
            "runtime": runtime,
        })
        _worker_emit({"event": "phase", "phase": "loaded_idle"})
        idle_start_cpu = time.process_time()
        idle_start_ns = time.monotonic_ns()
        time.sleep(args.idle_seconds)
        idle_end_cpu = time.process_time()
        idle_end_ns = time.monotonic_ns()
        worker_result["loaded_idle"] = {
            "wall_seconds": round((idle_end_ns - idle_start_ns) / 1_000_000_000, 6),
            "cpu_seconds": round(idle_end_cpu - idle_start_cpu, 6),
            "cpu_mean_percent": _mean_cpu_percent(
                idle_end_cpu - idle_start_cpu, (idle_end_ns - idle_start_ns) / 1_000_000_000
            ),
        }

        worker_result["items"] = []
        for item in items:
            if _CANCEL_REQUESTED:
                raise WorkerCancelled
            sample_id = item["id"]
            stage = "phonemizer"
            def set_sample_phase(name: str) -> None:
                nonlocal stage
                stage = "phonemizer" if name == "phonemization" else "inference"
                _worker_emit({"event": "phase", "phase": f"{name}:{sample_id}"})

            sample_cpu_started = time.process_time()
            phonemes, audio, phonemize_ms, inference_ms = _synthesize_text(
                pipeline, voice_tensor, item["text"], on_phase=set_sample_phase
            )
            sample_cpu_seconds = time.process_time() - sample_cpu_started

            stage = "wav_write"
            _worker_emit({"event": "phase", "phase": f"wav_write:{sample_id}"})
            wav_path = args.output_dir / f"{sample_id}.wav"
            try:
                wav_info = _atomic_write_wav(wav_path, audio, sf, np, SAMPLE_RATE_HZ)
            except BenchmarkError:
                raise
            except Exception:
                raise BenchmarkError("audio_write_failed") from None
            generation_ms = round(phonemize_ms + inference_ms, 3)
            sample_info = {
                "id": sample_id,
                "category": item["category"],
                "text_chars": len(item["text"]),
                "phoneme_chars": len(phonemes),
                "phonemization_ms": round(phonemize_ms, 3),
                "inference_ms": round(inference_ms, 3),
                "generation_ms": generation_ms,
                "cpu_seconds": round(sample_cpu_seconds, 6),
                "cpu_mean_percent": _mean_cpu_percent(sample_cpu_seconds, generation_ms / 1000),
                "audio_duration_seconds": round(wav_info["audio_duration_seconds"], 6),
                "real_time_factor": round(generation_ms / 1000 / wav_info["audio_duration_seconds"], 6),
                **{key: value for key, value in wav_info.items() if key != "audio_duration_seconds"},
            }
            sample_info["event"] = "sample_complete"
            sample_info["received_ns"] = time.monotonic_ns()
            _worker_emit(sample_info)
            worker_result["items"].append(sample_info)

        stage = "shutdown"
    except WorkerCancelled:
        error_code = "interrupted"
    except BenchmarkError as exc:
        error_code = exc.code
    except BaseException:
        error_code = {
            "dependency": "dependency_unavailable",
            "model_load": "model_load_failed",
            "phonemizer": "phonemizer_failed",
            "inference": "synthesis_failed",
            "wav_write": "audio_write_failed",
        }.get(stage, "benchmark_failed")
    finally:
        _worker_emit({"event": "phase", "phase": "shutdown"})
        with contextlib.suppress(Exception):
            del voice_tensor
        with contextlib.suppress(Exception):
            del pipeline
        with contextlib.suppress(Exception):
            del model
        gc.collect()

    if error_code:
        _worker_emit({"event": "worker_error", "code": error_code})
        return 130 if error_code == "interrupted" else 1
    _worker_emit({
        "event": "worker_result",
        "model_load_ms": round(model_load_ms, 3),
        "model_load_cpu_seconds": round(model_load_cpu_seconds, 6),
        "loaded_idle": worker_result.get("loaded_idle"),
        "runtime": runtime,
        "synthesized_count": len(worker_result.get("items", [])),
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "fetch-assets":
            fetch_assets(args)
            print(json.dumps({"status": "verified", "downloaded": True, "model_id": args.model_id, "voice": args.voice}))
            return 0
        if args.command == "verify-assets":
            _validate_candidate(args)
            paths = verify_assets(args.model_dir)
            print(json.dumps({"status": "verified", "asset_count": len(paths), "downloaded": False}))
            return 0
        if args.command == "run":
            metrics = run_benchmark(args)
            print(json.dumps({"status": metrics["status"], "metrics_file": "metrics.json", "synthesized": len(metrics["samples"])}))
            return 0
        if args.command == "_worker":
            return _worker_main(args)
        raise BenchmarkError("benchmark_failed")
    except BenchmarkError as exc:
        print(json.dumps({"status": "error", "error_code": exc.code, "message": ERROR_MESSAGES[exc.code]}), file=sys.stderr)
        return 130 if exc.code == "interrupted" else 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "error", "error_code": "interrupted", "message": ERROR_MESSAGES["interrupted"]}), file=sys.stderr)
        return 130
    except Exception:
        print(json.dumps({"status": "error", "error_code": "benchmark_failed", "message": ERROR_MESSAGES["benchmark_failed"]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
