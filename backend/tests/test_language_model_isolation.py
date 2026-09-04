"""Structural isolation proof: no Groq symbols above the LanguageModel adapter.

Issue #337: Katherine is not a specific model. The domain modules
(engine, process_turn, companion_runtime, main) and the desktop shell
must depend only on the canonical ``LanguageModel`` contract
(``backend/language_model.py``). The Groq implementation lives behind
the explicit adapter (``backend/groq_language_model.py``) and the
existing manager (``backend/groq_manager.py``), reached exclusively
through composition roots and the adapter itself.

This is an AST-level structural test (not a text grep) so it is robust
against formatting, but its allowlist is deliberately explicit so it
never bans the adapter, the composition root, or the adapter's own
tests — only the domain.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Modules where Groq symbols are FORBIDDEN: the Katherine domain and
#: the desktop shell. The contract boundary is exactly here.
_FORBIDDEN_MODULES = [
    "backend/engine.py",
    "backend/process_turn.py",
    "backend/companion_runtime.py",
    "backend/main.py",
    "backend/chat_engine.py",
    "backend/dependencies.py",
    "backend/health.py",
]

#: Desktop shell: pure bridge, no provider at all.
_FORBIDDEN_DESKTOP_DIR = "backend/desktop"

#: Where Groq legitimately lives: manager + adapter + key loader +
#: composition wiring is allowed to *name* the adapter factory.
_GROQ_HOME = {
    "backend/groq_manager.py",
    "backend/groq_language_model.py",
    "backend/groq_keys.py",
}

_BANNED_MODULES = ("groq", "backend.groq_manager", "backend.groq_keys", "backend.groq_language_model")

_BANNED_NAME_PREFIXES = ("Groq",)


def _iter_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno, None
        elif isinstance(node, ast.ImportFrom):
            yield (node.module or ""), node.lineno, node.level


def _groq_import_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for module, lineno, level in _iter_imports(tree):
        if level:  # relative import: resolve against package root
            module = f"backend.{module}" if module else "backend"
        if module in _BANNED_MODULES or module.split(".")[0] == "groq":
            violations.append(f"{path}:{lineno}: imports Groq module {module!r}")
    return violations


def _groq_toplevel_import_violations(path: Path) -> list[str]:
    """Groq imports at *module import time* only (function-level lazy
    imports inside composition-root factories are allowed by design)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    # Only statements directly in the module body execute when the
    # module is imported; imports nested inside functions run at call
    # time (the sanctioned lazy mechanism for the composition root).
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module in _BANNED_MODULES or module.split(".")[0] == "groq":
                    violations.append(
                        f"{path}:{node.lineno}: imports Groq module {module!r} at import time"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = f"backend.{node.module}" if node.level else (node.module or "")
            if module in _BANNED_MODULES or module.split(".")[0] == "groq":
                violations.append(
                    f"{path}:{node.lineno}: imports Groq module {module!r} at import time"
                )
    return violations


def _groq_name_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith(_BANNED_NAME_PREFIXES):
            violations.append(f"{path}:{node.lineno}: references {node.id}")
        elif isinstance(node, ast.Attribute) and isinstance(
            node.value, ast.Name
        ) and node.value.id.startswith(_BANNED_NAME_PREFIXES):
            violations.append(f"{path}:{node.lineno}: references {node.value.id}.{node.attr}")
    return violations


def test_domain_modules_never_import_groq():
    """engine/process_turn/companion_runtime/main depend only on the contract."""
    violations: list[str] = []
    for rel in _FORBIDDEN_MODULES:
        path = REPO_ROOT / rel
        assert path.exists(), f"expected module {rel} to exist"
        violations.extend(_groq_import_violations(path))
    assert violations == [], (
        "Groq leaked above the adapter into the domain:\n"
        + "\n".join(violations)
        + "\nThe domain must depend on backend.language_model only."
    )


def test_domain_modules_never_reference_groq_names():
    """No Groq* symbol (manager, exceptions, SDK types) in domain code."""
    violations: list[str] = []
    for rel in _FORBIDDEN_MODULES:
        path = REPO_ROOT / rel
        violations.extend(_groq_name_violations(path))
    assert violations == [], (
        "Groq symbols referenced above the adapter:\n" + "\n".join(violations)
    )


def test_desktop_shell_never_imports_groq():
    """The desktop shell stays a pure bridge, except the composition root.

    ``backend/desktop/app.py`` is the desktop composition root (issue
    #337 review): it is the one desktop file allowed to select the
    concrete provider, capture its keys Python-side and build the
    lazy adapter factory. Every other desktop module must stay
    provider-free, and even the composition root must not import the
    provider SDK at import time (the factory defers it).
    """
    violations: list[str] = []
    desktop_dir = REPO_ROOT / _FORBIDDEN_DESKTOP_DIR
    composition_root = desktop_dir / "app.py"
    for path in sorted(desktop_dir.rglob("*.py")):
        if path == composition_root:
            # Composition root: explicit provider selection is its job.
            # It must never IMPORT the SDK/manager/keys at module level
            # (import-time purity is what keeps startup SDK-free); the
            # lazy, function-level imports inside the factory builder
            # are its sanctioned mechanism.
            violations.extend(_groq_toplevel_import_violations(path))
            continue
        violations.extend(_groq_import_violations(path))
        violations.extend(_groq_name_violations(path))
    assert violations == [], (
        "Desktop shell leaked provider code:\n" + "\n".join(violations)
    )


def test_groq_home_is_exactly_the_adapter_and_manager():
    """Positive control: Groq lives in the adapter/manager modules, which
    MUST exist and MUST import the manager (this keeps the test honest
    rather than vacuously green)."""
    adapter = REPO_ROOT / "backend/groq_language_model.py"
    manager = REPO_ROOT / "backend/groq_manager.py"
    assert manager.exists(), "backend/groq_manager.py must exist (provider home)"
    assert adapter.exists(), "backend/groq_language_model.py must exist (the adapter)"
    tree = ast.parse(adapter.read_text(encoding="utf-8"))
    imported = set()
    for module, _, _ in _iter_imports(tree):
        imported.add(module)
    assert "backend.groq_manager" in imported or any(
        m.startswith("groq") for m in imported
    ), "the adapter must actually import the Groq manager/SDK (inside the boundary)"


def test_contract_module_has_no_groq_imports():
    """The canonical contract is provider-agnostic by construction."""
    contract = REPO_ROOT / "backend/language_model.py"
    assert contract.exists(), "backend/language_model.py must exist (the contract)"
    assert _groq_import_violations(contract) == [], (
        "the LanguageModel contract must not import any provider SDK"
    )
    assert _groq_name_violations(contract) == [], (
        "the LanguageModel contract must not reference Groq symbols"
    )


def test_companion_runtime_is_provider_agnostic():
    """The desktop runtime must not know any concrete provider (review).

    Issue #337 review: the concrete choice (groq, Groq keys, Groq
    adapter) happens in the desktop composition root, never inside
    ``backend/companion_runtime.py``. This structural test fails if
    the runtime ever grows ``groq``/``groq_keys``/
    ``GroqClientManager``/``GroqLanguageModel`` references again.
    It deliberately does not forbid those names in the adapter,
    manager, keys loader, composition roots or adapter tests.
    """
    runtime_path = REPO_ROOT / "backend/companion_runtime.py"
    assert runtime_path.exists()
    text = runtime_path.read_text(encoding="utf-8")
    banned = (
        "groq",
        "groq_keys",
        "GroqClientManager",
        "GroqLanguageModel",
    )
    violations = [name for name in banned if name in text]
    assert violations == [], (
        "companion_runtime.py must stay provider-agnostic; "
        f"found provider references: {violations}. Move the concrete "
        "provider wiring to the desktop composition root "
        "(backend/desktop/app.py)."
    )
