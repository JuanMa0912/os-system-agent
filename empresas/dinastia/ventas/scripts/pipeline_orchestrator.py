#!/usr/bin/env python3
"""
MAIN ORCHESTRATOR — Dinastia ventas (rentabilidad por línea) pipeline.

Adapted from ``_reference/ventas/pipeline_orchestrator.py``. Same shape:
run light ETLs in parallel + heavy ETLs sequentially, with per-ETL retries,
structured logging, and a JSON run report. Differences are cosmetic here (the
MySQL/GCP specifics live in the ETLs and ``common/``, not the orchestrator):

- Pulls shared helpers from ``common.utils`` (config, logger, dates, JSON report).
- Passes the config path down to each ETL via the runner (``DINASTIA_ETL_CONFIG``).

Modes:
  --mode daily    -> load yesterday (single-day range).
  --mode monthly  -> reprocess month-to-date, but only on ``validation_weeks``.

Exit codes: 0 all ETLs ok · 1 one or more ETLs failed · 2 fatal orchestrator error.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List

# Make `common` and sibling scripts importable regardless of CWD.
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
for p in (str(ROOT_DIR), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common.utils import (  # noqa: E402
    PipelineConfig,
    PipelineLogger,
    cleanup_old_logs,
    format_duration,
    get_month_range_yyyymmdd,
    get_yesterday_yyyymmdd,
    should_run_validation,
    write_json_report,
)
from etl_runner import ETLResult, ETLRunner  # noqa: E402

DEFAULT_CONFIG = "/opt/dinastia-ventas/config/pipeline_config.yaml"


class PipelineOrchestrator:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = PipelineConfig(config_path)
        self.logger = PipelineLogger(self.config, "orchestrator")
        self.etl_runner = ETLRunner(
            etl_dir=Path(self.config.require("paths.etl_dir")),
            max_retries=self.config.get("execution.max_retries", 3),
            retry_delay=self.config.get("execution.retry_delay_seconds", 60),
            timeout=self.config.get("execution.etl_timeout", 1800),
            logger=self.logger,
            python_executable=self.config.get("execution.python_executable"),
            config_path=config_path,
        )
        self.results: List[ETLResult] = []

    # -- run modes ----------------------------------------------------------
    def run_daily_load(self) -> None:
        self._banner("STARTING DAILY VENTAS LOAD")
        target = get_yesterday_yyyymmdd(self.config.timezone)
        self.logger.info(f"Date to process: {target}")
        self._cleanup_logs()
        started = datetime.now()
        self._run_parallel_etls(target, target)
        self._run_sequential_etls(target, target)
        self._report((datetime.now() - started).total_seconds(), "DAILY")

    def run_monthly_validation(self) -> None:
        self._banner("STARTING MONTHLY VALIDATION")
        weeks = self.config.get("execution.validation_weeks", [1, 3])
        if not should_run_validation(self.config.timezone, weeks):
            self.logger.info("Today is not a validation week. Aborting.")
            return
        start_date, end_date = get_month_range_yyyymmdd(self.config.timezone)
        self.logger.info(f"Range to validate: {start_date} - {end_date}")
        self._cleanup_logs()
        started = datetime.now()
        self._run_parallel_etls(start_date, end_date)
        self._run_sequential_etls(start_date, end_date)
        self._report((datetime.now() - started).total_seconds(), "MONTHLY_VALIDATION")

    # -- execution ----------------------------------------------------------
    def _run_parallel_etls(self, start_date: str, end_date: str) -> None:
        etls = sorted(self.config.get("etls.parallel", []) or [],
                      key=lambda x: x.get("priority", 999))
        if not etls:
            self.logger.warning("No parallel ETLs configured")
            return
        self.logger.info(f"Running {len(etls)} ETL(s) in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(etls)) as pool:
            futures = {
                pool.submit(self.etl_runner.run_etl, e["script"], e["name"],
                            start_date, end_date): e["name"]
                for e in etls
            }
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    self.results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(f"Error running {name}: {exc}", exc_info=True)

    def _run_sequential_etls(self, start_date: str, end_date: str) -> None:
        etls = sorted(self.config.get("etls.sequential", []) or [],
                      key=lambda x: x.get("priority", 999))
        if not etls:
            self.logger.info("No sequential ETLs configured")
            return
        self.logger.info(f"Running {len(etls)} sequential ETL(s)...")
        for e in etls:
            self.results.append(
                self.etl_runner.run_etl(e["script"], e["name"], start_date, end_date)
            )

    # -- housekeeping & reporting ------------------------------------------
    def _cleanup_logs(self) -> None:
        logs_dir = Path(self.config.require("paths.logs_dir"))
        retention = self.config.get("logging.retention_days", 14)
        self.logger.info(f"Cleaning logs older than {retention} days...")
        cleanup_old_logs(logs_dir, retention, self.logger.logger)

    def _banner(self, text: str) -> None:
        self.logger.info("=" * 80)
        self.logger.info(text)
        self.logger.info("=" * 80)

    def _report(self, total_duration: float, run_type: str) -> None:
        self._banner(f"FINAL REPORT - {run_type}")
        ok = sum(1 for r in self.results if r.success)
        failed = len(self.results) - ok
        total_records = sum(r.records_processed for r in self.results)

        self.logger.info(f"Total duration: {format_duration(total_duration)}")
        self.logger.info(f"ETLs executed: {len(self.results)}  (ok={ok} failed={failed})")
        self.logger.info(f"Records processed: {total_records:,}")
        self.logger.info("-" * 80)
        for r in sorted(self.results, key=lambda r: r.name):
            symbol = "OK " if r.success else "ERR"
            self.logger.info(
                f"[{symbol}] {r.name:18s} {r.status.value.upper():8s} | "
                f"dur={format_duration(r.duration_seconds):>8s} | "
                f"rows={r.records_processed:>7,} | attempts={r.attempts}"
            )
            if not r.success and r.error_message:
                self.logger.error(f"    Error: {r.error_message}")

        try:
            report_file = write_json_report(
                Path(self.config.require("paths.logs_dir")), run_type,
                self.results, self.config.timezone,
            )
            self.logger.info(f"JSON report saved: {report_file}")
        except OSError as exc:
            self.logger.error(f"Could not write JSON report: {exc}")

        self._banner(
            "PIPELINE COMPLETED SUCCESSFULLY" if failed == 0
            else f"PIPELINE COMPLETED WITH {failed} ERROR(S)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dinastia ventas pipeline orchestrator")
    parser.add_argument("--mode", choices=["daily", "monthly"], required=True,
                        help="daily = yesterday; monthly = month-to-date validation")
    parser.add_argument(
        "--config",
        default=os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG),
        help="Path to the pipeline config YAML "
             "(or set DINASTIA_ETL_CONFIG).",
    )
    args = parser.parse_args()

    try:
        orch = PipelineOrchestrator(args.config)
        if args.mode == "daily":
            orch.run_daily_load()
        else:
            orch.run_monthly_validation()
        sys.exit(1 if any(not r.success for r in orch.results) else 0)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
