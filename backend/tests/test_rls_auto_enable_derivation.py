"""Offline unit tests for the migration-version derivation in
test_rls_auto_enable_legacy.py (#291).

These tests never talk to Supabase, Docker or the network. They pin the
derivation logic that replaces the previously hard-coded migration timestamp:
renaming the migration file must not silently leave the suite checking a
stale version, and the filename matching must tolerate the CI
``.legacy-test-hidden`` and direct-run ``.tmp`` hidden states.
"""

from pathlib import Path

from backend.tests.test_rls_auto_enable_legacy import (
    LEGACY_HIDDEN_SUFFIX,
    MIGRATION,
    MIGRATION_VERSION,
    _HARDEN_FILENAME_RE,
    _find_harden_migration,
    _version_from_filename,
)

_BASE_NAME = "20260807201256_harden_rls_auto_enable.sql"


def test_version_is_the_fixed_width_prefix_of_the_file_name():
    """The derived version must be the file name's prefix, not a constant."""
    assert Path(MIGRATION).name.startswith(MIGRATION_VERSION + "_")
    assert MIGRATION_VERSION.isdigit()


def test_version_from_filename():
    assert _version_from_filename(_BASE_NAME) == "20260807201256"
    assert _version_from_filename("20240101000006_process_turn_replay.sql") == (
        "20240101000006"
    )


def test_filename_regex_accepts_live_and_hidden_states():
    for name in (
        _BASE_NAME,
        f"{_BASE_NAME}.tmp",
        f"{_BASE_NAME}{LEGACY_HIDDEN_SUFFIX}",
    ):
        assert _HARDEN_FILENAME_RE.match(name), f"regex rejected {name!r}"


def test_filename_regex_rejects_unrelated_migrations():
    for name in (
        "20240101000006_process_turn_replay.sql",
        "20260807201256_some_other_migration.sql",
        "harden_rls_auto_enable.sql",
        "20260807201256_harden_rls_auto_enable.sql.bak",
    ):
        assert not _HARDEN_FILENAME_RE.match(name), f"regex accepted {name!r}"


def test_find_harden_migration_resolves_the_live_file():
    found = _find_harden_migration()
    assert _HARDEN_FILENAME_RE.match(found.name)
    assert found.name.endswith("_harden_rls_auto_enable.sql")
    # The version the test suite will check must come from the same file.
    assert _version_from_filename(str(found)) == MIGRATION_VERSION
