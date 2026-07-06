#!/usr/bin/env python3
"""collect_etl_status.py — gather ETL status and render the daily report.

Two modes:

* ``--dry-run`` (default): mock each job's latest run as ``now - 30min`` and
  render the report locally — no server access needed.
* ``--live``: read real status over read-only SSH. For each job with a
  ``systemd_unit``, run ``systemctl show`` on the server (allowlist-enforced),
  parse the last-run result/timestamp, and classify freshness.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from os_system_agent.catalog import EtlJob, load_catalog
from os_system_agent.monitors.freshness import JobStatus, evaluate_freshness
from os_system_agent.monitors.systemd import evaluate_systemd, parse_state, show_command
from os_system_agent.reports.daily import render_daily_report
from os_system_agent.ssh_client import run_read_only

DEFAULT_CATALOG = Path("config/alert-rules.example.yml")
DEFAULT_SERVER_ALIAS = "server232"
MOCK_AGE_MINUTES = 30


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ETL status and render a daily report.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--server-alias", default=DEFAULT_SERVER_ALIAS)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--live", dest="dry_run", action="store_false")
    return parser.parse_args(argv)


def _collect_dry_run(jobs: list[EtlJob], now: datetime) -> list[JobStatus]:
    mocked_latest = now - timedelta(minutes=MOCK_AGE_MINUTES)
    return [evaluate_freshness(job, mocked_latest, now) for job in jobs]


def _collect_live(jobs: list[EtlJob], alias: str, now: datetime) -> list[JobStatus]:
    statuses: list[JobStatus] = []
    for job in jobs:
        if not job.systemd_unit:
            continue
        result = run_read_only(alias, show_command(job.systemd_unit))
        state = parse_state(result.stdout, job.systemd_unit)
        statuses.append(evaluate_systemd(job, state, now))
    return statuses


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    jobs = load_catalog(args.catalog)
    now = datetime.now(UTC)

    if args.dry_run:
        statuses = _collect_dry_run(jobs, now)
    else:
        statuses = _collect_live(jobs, args.server_alias, now)

    server = jobs[0].server if jobs else args.server_alias
    report = render_daily_report(
        server=server,
        report_date=date.fromisoformat(now.date().isoformat()),
        statuses=statuses,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
