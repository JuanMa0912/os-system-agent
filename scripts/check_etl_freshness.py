#!/usr/bin/env python3
"""check_etl_freshness.py — evaluate whether expected files/tables are fresh.

Phase 1 (T006): dry-run by default. Loads the catalog and prints one freshness
line per job using a mocked latest timestamp (``now - <age> min``). Classification
logic lives in ``os_system_agent.monitors.freshness`` / ``severity``.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from os_system_agent.catalog import load_catalog
from os_system_agent.monitors.freshness import evaluate_freshness
from os_system_agent.redaction import redact

DEFAULT_CATALOG = Path("config/alert-rules.example.yml")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate ETL freshness per job (dry-run).")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help="Path to the ETL job catalog YAML (default: %(default)s).",
    )
    parser.add_argument(
        "--age-minutes",
        type=float,
        default=30.0,
        help="Mocked age of the latest output, in minutes (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    jobs = load_catalog(args.catalog)
    now = datetime.now(UTC)
    mocked_latest = now - timedelta(minutes=args.age_minutes)
    for job in jobs:
        status = evaluate_freshness(job, mocked_latest, now)
        print(f"{status.severity.value:<8} {redact(status.name)} — {redact(status.evidence)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
