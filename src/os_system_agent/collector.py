"""Collect ETL statuses and render the daily report.

Shared by both entrypoints — the CLI collector (``collect_etl_status.py``) and
the Telegram push (``send_daily_report.py``) — so there is exactly one
implementation to test. No new I/O beyond the read-only SSH runner it is given.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from os_system_agent.catalog import EtlJob
from os_system_agent.monitors.freshness import JobStatus, evaluate_freshness
from os_system_agent.monitors.systemd import (
    SystemdState,
    evaluate_systemd,
    parse_multi,
    show_command_multi,
)
from os_system_agent.reports.daily import render_chat_report, render_daily_report
from os_system_agent.ssh_client import CommandResult, run_read_only

# In dry-run we pretend every job finished this long ago (well inside any
# freshness window) so the report renders without touching a server.
MOCK_AGE_MINUTES = 30

# A read-only command runner: (alias, command) -> CommandResult. Injected in
# tests so live collection can be exercised without a real server.
Runner = Callable[[str, str], CommandResult]


def collect_dry_run(jobs: list[EtlJob], now: datetime) -> list[JobStatus]:
    """Evaluate each job against a mocked recent timestamp (no server access)."""
    mocked_latest = now - timedelta(minutes=MOCK_AGE_MINUTES)
    return [evaluate_freshness(job, mocked_latest, now) for job in jobs]


def collect_live(
    jobs: list[EtlJob],
    alias: str,
    now: datetime,
    *,
    runner: Runner = run_read_only,
) -> list[JobStatus]:
    """Read real last-run status over read-only SSH in ONE ``systemctl show`` call."""
    pairs: list[tuple[EtlJob, str]] = [
        (job, job.systemd_unit) for job in jobs if job.systemd_unit
    ]
    if not pairs:
        return []

    units = [unit for _, unit in pairs]
    result = runner(alias, show_command_multi(units))
    states = parse_multi(result.stdout)

    statuses: list[JobStatus] = []
    for job, unit in pairs:
        state = states.get(unit) or SystemdState(
            unit=unit,
            result="unknown",  # unit not in output -> fail closed (CRITICAL)
            exit_status=None,
            last_exit_at=None,
            active_state=None,
        )
        statuses.append(evaluate_systemd(job, state, now))
    return statuses


def collect_statuses(
    jobs: list[EtlJob],
    *,
    live: bool,
    alias: str,
    now: datetime,
    runner: Runner = run_read_only,
) -> list[JobStatus]:
    """Collect job statuses either live (SSH) or via the dry-run mock."""
    if live:
        return collect_live(jobs, alias, now, runner=runner)
    return collect_dry_run(jobs, now)


def build_daily_report(jobs: list[EtlJob], statuses: list[JobStatus], now: datetime) -> str:
    """Render the full §13 daily report (evidence already redacted)."""
    server = jobs[0].server if jobs else "unknown"
    return render_daily_report(
        server=server,
        report_date=now.date(),
        statuses=statuses,
    )


def build_chat_report(jobs: list[EtlJob], statuses: list[JobStatus], now: datetime) -> str:
    """Render the compact, chat-friendly report (for Telegram push + pull)."""
    server = jobs[0].server if jobs else "unknown"
    return render_chat_report(
        server=server,
        report_date=now.date(),
        statuses=statuses,
    )
