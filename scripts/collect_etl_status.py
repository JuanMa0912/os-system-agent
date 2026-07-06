#!/usr/bin/env python3
"""collect_etl_status.py — gather ETL status and render the daily report.

Two modes:

* ``--dry-run`` (default): mock each job's latest run as ``now - 30min`` and
  render the report locally — no server access needed.
* ``--live``: read real status over read-only SSH. For each job with a
  ``systemd_unit``, run ``systemctl show`` on the server (allowlist-enforced),
  parse the last-run result/timestamp, and classify freshness.

Collection and rendering live in :mod:`os_system_agent.collector` so this CLI
and the Telegram push (``send_daily_report.py``) share one implementation.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from os_system_agent.catalog import load_catalog
from os_system_agent.collector import build_daily_report, collect_statuses

DEFAULT_CATALOG = Path("config/alert-rules.example.yml")
DEFAULT_SERVER_ALIAS = "server232"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ETL status and render a daily report.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--server-alias", default=DEFAULT_SERVER_ALIAS)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--live", dest="dry_run", action="store_false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    jobs = load_catalog(args.catalog)
    now = datetime.now(UTC)

    statuses = collect_statuses(
        jobs,
        live=not args.dry_run,
        alias=args.server_alias,
        now=now,
    )
    print(build_daily_report(jobs, statuses, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
