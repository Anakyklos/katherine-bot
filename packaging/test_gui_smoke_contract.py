from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).with_name("gui_smoke_deb.sh")


def test_gui_smoke_reports_complete_resource_and_network_evidence() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "smaps_rollup",
        "pss_kib",
        "startup_rss_peak_kib",
        "frontend_bundle_kib",
        "initial_db_bytes",
        "idle_process_count",
        "idle_thread_count",
        "idle_snapshot_rss_kib",
        "idle_snapshot_pss_kib",
        "idle_outbound_network",
        "idle_internet_syscalls",
        "strace",
        "log_growth_bytes",
        "cache_growth_bytes",
        "process_breakdown",
    ):
        assert marker in source, marker
