# Katherine Bot

A sophisticated AI chatbot application featuring a React frontend and a Python backend, designed to provide an engaging and emotionally responsive user experience.

## 🚀 Project Structure

The project is divided into two main components:

- **frontend/**: A modern web interface built with React, Vite, and Tailwind CSS.
- **backend/**: A robust Python backend powering the chat logic, memory systems, and integrations.

## 🛠️ Setup & Installation

### Backend

Python dependencies are managed with [uv](https://docs.astral.sh/uv/). The authoritative graph is `backend/pyproject.toml` + `backend/uv.lock`; do not install backend dependencies with pip in your global Python.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (one-time, per machine).
2. Sync the locked environment from the repository root (or inside `backend/`):

   ```bash
   uv sync --project backend
   ```

   This creates `backend/.venv` with Python 3.12, the runtime dependencies (CPU-only PyTorch) and the test group, exactly as locked in `backend/uv.lock`. `--frozen` is implicit in CI; locally `uv sync` is enough.

3. Run anything through the managed environment with `uv run`:

   ```bash
   uv run --project backend python -m backend.serve --host 127.0.0.1 --port 8000   # production entrypoint
   uv run --project backend python -m pytest backend/tests                          # test suite
   ```

   For the development entrypoint: `uv run --project backend python backend/main.py`.

> **Production containment:** `uv run` wraps the same production entrypoint
> (`python -m backend.serve`) validated in CI. See
> [Production Containment](docs/operations/production-containment.md).

### Managing dependencies

```bash
cd backend

# add a runtime dependency (updates pyproject.toml + uv.lock)
uv add "package==x.y.z"

# add a test-only dependency
uv add --group test "package==x.y.z"

# remove a dependency
uv remove "package"          # or: uv remove --group test "package"

# verify the lock matches pyproject.toml WITHOUT modifying it (CI gate)
uv lock --check

# update the lock deliberately, then refresh the Docker compatibility export
uv lock
uv export --frozen --no-emit-project --no-hashes \
    --emit-index-url --no-group test --output-file requirements.txt
```

`backend/requirements.txt` is a **generated** export for the Docker build only — never edit it by hand (a test regenerates it and fails on drift).

The PyTorch CPU index is configured as an *explicit*, torch-only source in `pyproject.toml`; the lock always resolves the CPU-only wheels (`torch==X.Y.Z+cpu`, no CUDA/NVIDIA packages).

### Desktop Shell (Linux)

```bash
uv run --project backend python -m backend.desktop.app
```

Opens the production frontend build in a native GTK window (WebKitGTK)
via `file://` — no HTTP server. The companion conversation runs fully
local (#336): no login, no Supabase, LocalStorage SQLite as the only
persistence (`~/.local/share/katherine/`), and the remote LLM (Groq)
as the only network dependency. Local privacy operations (delete
history/memories, reset emotional/relationship state) are available in
the desktop sidebar. See
[Desktop Shell no Linux](docs/operations/desktop-shell-linux.md).

#### Linux `.deb`

To build and install the reproducible desktop package:

```bash
cd frontend && npm ci && npm run build && cd ..
SOURCE_DATE_EPOCH=1700000000 python3.12 packaging/build_deb.py \
  --version 0.1.0 --out-dir dist/deb
sudo dpkg -i dist/deb/katherine-desktop_0.1.0_amd64.deb
sudo apt-get -f install
```

The package runs the same pywebview/WebKitGTK shell and keeps user data in
`~/.local/share/katherine/`; it never packages or replaces `katherine.db`.
See the [Linux package operations guide](docs/operations/desktop-shell-linux.md#pacote-deb-do-desktop-338)
for native dependencies, lifecycle evidence, rollback, and benchmarks.

### Frontend

1. Navigate to the `frontend` directory:
   ```bash
   cd frontend
   ```
2. Install dependencies:
   ```bash
   npm install
   ```
3. Start the development server:
   ```bash
   npm run build
   ```

## ✨ Features

- **Interactive Chat**: Real-time messaging interface.
- **Emotional Intelligence**: Tracks and responds to emotional context.
- **Memory System**: Persistent context for conversations (archival extraction disabled by default).
- **Modern UI**: Clean, responsive design using Tailwind CSS.


## CI Commands Reference

The following commands mirror the CI pipeline (reproducible, locked environment):

### Backend Setup and Tests
```bash
# Provision the locked environment (runtime + test group, Python 3.12, CPU-only PyTorch).
# --frozen fails if pyproject.toml and uv.lock diverge and never rewrites the lock.
uv sync --project backend --frozen

# Verify environment health
uv pip check --project backend

# Compile backend
uv run --project backend python -m compileall -q backend

# Verify CPU-only PyTorch
uv run --project backend python -c "import torch; assert '+cpu' in torch.__version__"
uv run --project backend python -c "import torch; assert torch.cuda.is_available() is False"

# Run backend tests globally in isolated single process
PYTHONPATH=. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  uv run --project backend python -m pytest backend/tests
```

### Lock Maintenance
```bash
cd backend
uv lock --check   # verify lock without modifying it (CI gate)
# after deliberate dependency changes:
uv lock
uv export --frozen --no-emit-project --no-hashes \
    --emit-index-url --no-group test --output-file requirements.txt
```

### Frontend Setup and Verification
```bash
cd frontend

# Install dependencies cleanly
npm ci

# Audit dependencies (generate JSON report without failing CI on vulnerabilities)
set +e; npm audit --json > audit.json; exit_code=$?; set -e

# Run linting
npm run lint

# Build frontend
npm run build
```
