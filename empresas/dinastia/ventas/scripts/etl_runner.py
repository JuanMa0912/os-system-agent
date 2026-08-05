"""
etl_runner — run one ETL script as a subprocess, with retries and timeout.

Adapted from ``_reference/ventas/etl_runner.py``. Differences:

- Uses ``sys.executable`` by default (configurable), so it runs under whatever
  interpreter launched the orchestrator — the uv/venv python on the Dinastia box
  — instead of a hardcoded ``python3``.
- Parses the shared ``RECORDS_MARKER`` ("RECORDS_LOADED:") from stdout (defined
  once in ``common.utils``) to learn how many rows an ETL loaded. Also accepts
  the legacy Mercamio ``Upsert:`` token so reference ETLs stay compatible.
- Passes ``DINASTIA_ETL_CONFIG`` through the child env so each ETL finds the same
  config file the orchestrator used.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

# Make `common` importable whether run from scripts/ or the ventas root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.utils import RECORDS_MARKER  # noqa: E402


class ETLStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ETLResult:
    name: str
    status: ETLStatus
    start_time: datetime
    end_time: Optional[datetime]
    duration_seconds: float
    records_processed: int
    attempts: int
    error_message: Optional[str]
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.status == ETLStatus.SUCCESS

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.duration_seconds,
            "records_processed": self.records_processed,
            "attempts": self.attempts,
            "error_message": self.error_message,
        }


class ETLRunner:
    """Runs ETL scripts with retry/backoff and a hard timeout."""

    def __init__(
        self,
        etl_dir: Path,
        max_retries: int = 3,
        retry_delay: int = 60,
        timeout: int = 1800,
        logger=None,
        python_executable: Optional[str] = None,
        config_path: Optional[str] = None,
    ):
        self.etl_dir = Path(etl_dir)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.logger = logger
        # Default to the current interpreter so venv/uv envs "just work".
        self.python_executable = python_executable or sys.executable
        self.config_path = config_path

    def run_etl(
        self,
        script_name: str,
        etl_name: str,
        start_date: str,
        end_date: Optional[str] = None,
        extra_args: Optional[Sequence[str]] = None,
    ) -> ETLResult:
        script_path = self.etl_dir / script_name
        if not script_path.exists():
            return self._error_result(etl_name, f"Script not found: {script_path}")

        end_date = end_date or start_date
        start_time = datetime.now()
        result: Optional[ETLResult] = None

        for attempt in range(1, self.max_retries + 1):
            if self.logger:
                self.logger.info(
                    f"[{etl_name}] Attempt {attempt}/{self.max_retries} | "
                    f"Range: {start_date} - {end_date}"
                )
            result = self._execute(
                script_path, etl_name, start_date, end_date, attempt, start_time,
                extra_args or [],
            )
            if result.success:
                if self.logger:
                    self.logger.info(
                        f"[{etl_name}] SUCCESS | Duration: {result.duration_seconds:.1f}s "
                        f"| Records: {result.records_processed}"
                    )
                return result

            if attempt < self.max_retries:
                if self.logger:
                    self.logger.warning(
                        f"[{etl_name}] FAILED (attempt {attempt}) | "
                        f"Retrying in {self.retry_delay}s..."
                    )
                time.sleep(self.retry_delay)
            elif self.logger:
                self.logger.error(
                    f"[{etl_name}] FAILED after {attempt} attempts | "
                    f"Error: {result.error_message}"
                )
        assert result is not None
        return result

    def _execute(
        self,
        script_path: Path,
        etl_name: str,
        start_date: str,
        end_date: str,
        attempt: int,
        overall_start: datetime,
        extra_args: Sequence[str],
    ) -> ETLResult:
        if start_date == end_date:
            cmd = [self.python_executable, str(script_path), "--date", start_date]
        else:
            cmd = [
                self.python_executable, str(script_path),
                "--start-date", start_date, "--end-date", end_date,
            ]
        cmd.extend(extra_args)

        # Propagate the config path to the child ETL.
        child_env = dict(os.environ)
        if self.config_path:
            child_env.setdefault("DINASTIA_ETL_CONFIG", str(self.config_path))

        attempt_start = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout, cwd=self.etl_dir, env=child_env,
            )
            duration = time.time() - attempt_start
            records = self._parse_records(proc.stdout)

            if proc.returncode == 0:
                return ETLResult(
                    name=etl_name, status=ETLStatus.SUCCESS, start_time=overall_start,
                    end_time=datetime.now(), duration_seconds=duration,
                    records_processed=records, attempts=attempt, error_message=None,
                    stdout=proc.stdout, stderr=proc.stderr,
                )
            return ETLResult(
                name=etl_name, status=ETLStatus.FAILED, start_time=overall_start,
                end_time=datetime.now(), duration_seconds=duration,
                records_processed=records, attempts=attempt,
                error_message=f"Exit code {proc.returncode}: {proc.stderr[-2000:]}",
                stdout=proc.stdout, stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            return ETLResult(
                name=etl_name, status=ETLStatus.TIMEOUT, start_time=overall_start,
                end_time=datetime.now(), duration_seconds=time.time() - attempt_start,
                records_processed=0, attempts=attempt,
                error_message=f"Timeout after {self.timeout}s", stdout="", stderr="",
            )
        except Exception as exc:  # noqa: BLE001 - surface any launch failure as a result
            return ETLResult(
                name=etl_name, status=ETLStatus.FAILED, start_time=overall_start,
                end_time=datetime.now(), duration_seconds=time.time() - attempt_start,
                records_processed=0, attempts=attempt, error_message=str(exc),
                stdout="", stderr=str(exc),
            )

    @staticmethod
    def _parse_records(stdout: str) -> int:
        """Read the row count from the ETL's ``RECORDS_LOADED:`` marker.

        Falls back to the legacy Mercamio ``Upsert:`` token. Returns 0 if neither
        marker is present (parsing failure never masks a real success/failure —
        the exit code decides status; this only annotates the row count)."""
        for token in (RECORDS_MARKER, "Upsert:"):
            for line in reversed(stdout.splitlines()):
                if token in line:
                    try:
                        return int(line.split(token, 1)[1].strip().split()[0].replace(",", ""))
                    except (ValueError, IndexError):
                        continue
        return 0

    def _error_result(self, etl_name: str, error: str) -> ETLResult:
        now = datetime.now()
        return ETLResult(
            name=etl_name, status=ETLStatus.FAILED, start_time=now, end_time=now,
            duration_seconds=0.0, records_processed=0, attempts=0,
            error_message=error, stdout="", stderr=error,
        )
