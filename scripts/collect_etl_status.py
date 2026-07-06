#!/usr/bin/env python3
"""collect_etl_status.py — gather ETL status and render the daily report.

Phase 1 (T006/T007): dry-run by default. In dry-run each job's latest output is
mocked as ``now - 30min`` and the daily report is printed locally. Live
collection (real read-only SSH checks) is not implemented yet — it fails closed
with a clear message rather than pretending to connect.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from os_system_agent.catalog import load_catalog
from os_system_agent.monitors.freshness import evaluate_freshness
from os_system_agent.reports.daily import render_daily_report

DEFAULT_CATALOG = Path("config/alert-rules.example.yml")
MOCK_AGE_MINUTES = 30


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ETL status and render a daily report.")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help="Path to the ETL job catalog YAML (default: %(default)s).",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Mock ETL freshness locally (default).",
    )
    parser.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Attempt real read-only collection (not implemented in Phase 1).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not args.dry_run:
        print("live collection not implemented yet (needs server access)")
        return 2

    jobs = load_catalog(args.catalog)
    now = datetime.now(UTC)
    mocked_latest = now - timedelta(minutes=MOCK_AGE_MINUTES)
    statuses = [evaluate_freshness(job, mocked_latest, now) for job in jobs]

    server = jobs[0].server if jobs else "server232"
    report = render_daily_report(
        server=server,
        report_date=date.fromisoformat(now.date().isoformat()),
        statuses=statuses,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
