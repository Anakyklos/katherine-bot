# Katherine Bot

A sophisticated AI chatbot application featuring a React frontend and a Python backend, designed to provide an engaging and emotionally responsive user experience.

## 🚀 Project Structure

The project is divided into two main components:

- **frontend/**: A modern web interface built with React, Vite, and Tailwind CSS.
- **backend/**: A robust Python backend powering the chat logic, memory systems, and integrations.

## 🛠️ Setup & Installation

### Backend

1. Navigate to the `backend` directory:
   ```bash
   cd backend
   ```
2. Create and activate a virtual environment (optional but recommended):
   ```bash
   python3 -m venv venv
   # Windows
   .\venv\Scripts\activate
   # Linux/Mac
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the server (development only):
   ```bash
   python main.py
   ```

   > **Production:** Use `python -m backend.serve` instead.
   > See [Production Containment](docs/operations/production-containment.md).

### Desktop Shell (Linux)

```bash
python -m backend.desktop.app
```

Opens the production frontend build in a native GTK window (WebKitGTK)
via `file://` — no HTTP server. See
[Desktop Shell no Linux](docs/operations/desktop-shell-linux.md).

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

The following commands are used in the CI pipeline to ensure reproducible and isolated environments:

### Backend Setup and Tests
```bash
# Create and activate environment
python3 -m venv .venv-ci
source .venv-ci/bin/activate

# Install dependencies (CPU-only PyTorch and transitive pins via pip-tools)
python -m pip install -r backend/requirements.txt
python -m pip install -r backend/requirements-test.txt

# Verify installation health
python -m pip check

# Compile backend
python -m compileall -q backend

# Verify CPU-only PyTorch
python -c "import torch; assert '+cpu' in torch.__version__"
python -c "import torch; assert torch.cuda.is_available() is False"

# Run backend tests globally in isolated single process
PYTHONPATH=. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest backend/tests
```

### Requirements Management

Dependencies are managed using `pip-tools`. To regenerate the lock files:
```bash
python -m pip install pip-tools
pip-compile backend/requirements.in -o backend/requirements.txt
pip-compile backend/requirements-test.in -o backend/requirements-test.txt
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
