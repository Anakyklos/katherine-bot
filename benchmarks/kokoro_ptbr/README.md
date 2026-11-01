# Kokoro PT-BR benchmark

This directory is an independent, CPU-only evidence tool for issue #370 / #332. It does not import Katherine's backend, start a service, or change the production dependency graph. The pinned candidate is `hexgrad/Kokoro-82M` v1.0 with voice `pf_dora`.

## Isolated environment

Use Python 3.12 and `uv`. The project has its own `pyproject.toml` and `uv.lock`; all commands target that project and its local `.venv`. Put uv's package cache outside the checkout:

```bash
CACHE_ROOT="$HOME/.cache/katherine-kokoro-benchmark"
uv sync --project benchmarks/kokoro_ptbr --locked \
  --cache-dir "$CACHE_ROOT/uv"
uv run --project benchmarks/kokoro_ptbr --locked \
  --cache-dir "$CACHE_ROOT/uv" \
  python -m pytest benchmarks/kokoro_ptbr/tests
```

The project pins the official `kokoro` inference package and CPU-only PyTorch. Upstream Kokoro currently declares `misaki[en]` as a dependency even for its Portuguese path; this installs the package's broader English G2P closure. Portuguese itself uses Misaki's `EspeakG2P(language="pt-br")`, backed by the pinned `phonemizer-fork` and `espeakng-loader` packages. The benchmark records those installed versions and the bundled eSpeak NG library version. It does not install system packages or alter the desktop shell.

## Explicit model acquisition

Model acquisition is a separate command and fails unless `--allow-downloads` is present. The public files are fetched at the immutable repository revision in `model_artifacts.json`, copied to the selected model directory, then checked against their pinned sizes and SHA-256 values. Public downloads explicitly disable Hugging Face token use and Xet, and keep Hub, Xet, and telemetry state under the selected cache directory. The run command never downloads files and forces Hugging Face and Transformers offline mode.

```bash
CACHE_ROOT="$HOME/.cache/katherine-kokoro-benchmark"
uv run --project benchmarks/kokoro_ptbr --locked --cache-dir "$CACHE_ROOT/uv" \
  python benchmarks/kokoro_ptbr/benchmark.py fetch-assets \
  --model-id hexgrad/Kokoro-82M \
  --model-revision f3ff3571791e39611d31c381e3a41a3af07b4987 \
  --voice pf_dora \
  --model-dir "$CACHE_ROOT/assets" \
  --cache-dir "$CACHE_ROOT/huggingface" \
  --allow-downloads
```

Verify without network access:

```bash
uv run --project benchmarks/kokoro_ptbr --locked --cache-dir "$CACHE_ROOT/uv" \
  python benchmarks/kokoro_ptbr/benchmark.py verify-assets \
  --model-id hexgrad/Kokoro-82M \
  --model-revision f3ff3571791e39611d31c381e3a41a3af07b4987 \
  --voice pf_dora \
  --model-dir "$CACHE_ROOT/assets"
```

If any pinned asset is missing or has the wrong hash, synthesis stops with a fixed error code and no network attempt. Existing mismatched files are preserved; the tool does not repair or overwrite them.

## Run the corpus

Every run needs an explicit model directory, corpus, output directory, Hugging Face cache directory, and uv cache directory. The output directory must be new or empty. WAVs and `metrics.json` stay outside Git. The default run waits two seconds with the loaded model before synthesis to measure steady loaded-idle resources. `--limit 2 --idle-seconds 0` is a short real-inference diagnostic.

```bash
uv run --project benchmarks/kokoro_ptbr --locked --cache-dir "$CACHE_ROOT/uv" \
  python benchmarks/kokoro_ptbr/benchmark.py run \
  --model-id hexgrad/Kokoro-82M \
  --model-revision f3ff3571791e39611d31c381e3a41a3af07b4987 \
  --voice pf_dora \
  --model-dir "$CACHE_ROOT/assets" \
  --corpus benchmarks/kokoro_ptbr/corpus.jsonl \
  --output-dir "$CACHE_ROOT/runs/diagnostic-01" \
  --cache-dir "$CACHE_ROOT/huggingface" \
  --uv-cache-dir "$CACHE_ROOT/uv" \
  --limit 2 --idle-seconds 0 --threads 4
```

Use another empty output directory and omit `--limit` for the full corpus. Do not put generated WAVs, downloaded weights, or logs in the repository.

## Measurements and limits

The parent records worker startup and first-WAV readiness on a monotonic clock. The worker reports model load, G2P, inference, WAV-write, and per-utterance timings. Per-utterance and loaded-idle CPU time use the child process CPU clock; a Linux `/proc` sampler polls worker RSS, PSS (`smaps_rollup`), and CPU time every 100 ms. CPU percentage uses one fully occupied logical CPU as 100%, so multithreaded use can exceed 100%. Disk values in JSON are apparent file bytes; `du -s` can be used to report allocated filesystem blocks. The report also separates model files, the Hugging Face cache, uv cache, installed benchmark environment, and generated WAVs.

The idle sample is taken while weights and voice remain loaded in the short-lived benchmark process. After the command returns, the worker and its process group have exited; no service remains. WAV generation does not use an audio device. The metrics report device-node/player detection but deliberately records human listening as `UNVERIFIED` until someone listens to and evaluates the files. To listen manually, use a local player such as `paplay <sample.wav>` when an output device is available.

These measurements do not create acceptance thresholds. Compare the measured incremental TTS process with the desktop figures in `docs/operations/desktop-shell-linux.md`; a standalone benchmark is not a combined WebKitGTK + TTS measurement.

## Provenance and licenses

- Model: [`hexgrad/Kokoro-82M`](https://huggingface.co/hexgrad/Kokoro-82M), v1.0, pinned commit and asset hashes in `model_artifacts.json`; model and voice files are Apache-2.0.
- Inference code: [`hexgrad/kokoro`](https://github.com/hexgrad/kokoro), PyPI `kokoro==0.9.4`, Apache-2.0.
- PT-BR G2P code: [`hexgrad/misaki`](https://github.com/hexgrad/misaki), locked through Kokoro's `misaki[en]` dependency.
- `phonemizer-fork` and `espeakng-loader` are separately pinned in `uv.lock`. eSpeak NG is GPL-3.0-or-later; record its bundled library version in the run output. The dependency closure is isolated to this research tool and is not added to Katherine's production package.
- The generated corpus is synthetic, versioned in `corpus.jsonl`, and contains no conversation data or recordings.
