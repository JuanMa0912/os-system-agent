"""
common.utils — configuration, logging, dates, and JSON reports.

Adapted from the Mercamio reference ``pipeline_utils.py``
(``dinastia-etl/_reference/ventas/pipeline_utils.py``) with three additions:

1. ``PipelineConfig`` expands ``${ENV_VAR}`` placeholders at load time so **no
   secret ever lives in the YAML file** — secrets come from the environment
   (systemd ``EnvironmentFile=`` on the Dinastia box).
2. CLI date-range parsing helpers (moved here from the per-category ETL so every
   pipeline parses ``--date`` / ``--start-date`` / ``--end-date`` identically).
3. JSON-report helpers + the ``RECORDS_MARKER`` stdout contract shared with
   ``scripts/etl_runner.py`` (how the orchestrator learns how many rows loaded).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import yaml

# ---------------------------------------------------------------------------
# stdout contract between an ETL and the runner
# ---------------------------------------------------------------------------
# Each ETL MUST print exactly one line ``RECORDS_LOADED: <n>`` on success.
# ``scripts/etl_runner.py`` greps stdout for this marker to report row counts.
# Keeping it in one place means ventas/rotacion/margen all stay in sync.
RECORDS_MARKER = "RECORDS_LOADED:"

# Matches ${VAR} or ${VAR:-default} inside YAML scalar values.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or a required env var is unset."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class PipelineConfig:
    """YAML configuration with dot-notation access and ``${ENV}`` expansion.

    Fail-closed: a ``${VAR}`` with no environment value and no ``:-default``
    raises :class:`ConfigError` (we never silently run with a blank password).
    """

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self._config = self._expand(self._load_config())

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            raise ConfigError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ConfigError(f"Config root must be a mapping: {self.config_path}")
        return data

    def _expand(self, value: Any) -> Any:
        """Recursively expand ``${ENV_VAR}`` placeholders in strings."""
        if isinstance(value, dict):
            return {k: self._expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand(v) for v in value]
        if isinstance(value, str):
            return self._expand_str(value)
        return value

    def _expand_str(self, raw: str) -> str:
        def _sub(match: re.Match) -> str:
            var, default = match.group(1), match.group(2)
            env_val = os.environ.get(var)
            if env_val is not None and env_val != "":
                return env_val
            if default is not None:
                return default
            raise ConfigError(
                f"Environment variable '{var}' is required by {self.config_path} "
                f"but is not set (referenced as '${{{var}}}')."
            )
        return _ENV_PLACEHOLDER.sub(_sub, raw)

    def get(self, key_path: str, default: Any = None) -> Any:
        """Fetch a value by dotted path, e.g. ``get('source.mysql.host')``."""
        value: Any = self._config
        for key in key_path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def require(self, key_path: str) -> Any:
        """Like :meth:`get` but raises if the key is absent (fail closed)."""
        sentinel = object()
        value = self.get(key_path, sentinel)
        if value is sentinel:
            raise ConfigError(f"Missing required config key: '{key_path}'")
        return value

    def section(self, key_path: str) -> dict:
        """Return a mapping section (or empty dict) at ``key_path``."""
        value = self.get(key_path, {})
        return value if isinstance(value, dict) else {}

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.get("timezone", "America/Bogota"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class PipelineLogger:
    """File + console logger. Mirrors the reference logger's format/behaviour."""

    DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, config: PipelineConfig, component_name: str):
        self.config = config
        self.component_name = component_name
        self.logs_dir = Path(config.require("paths.logs_dir"))
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(self.component_name)
        logger.setLevel(logging.DEBUG)
        if logger.handlers:  # avoid duplicate handlers on re-instantiation
            return logger

        fmt = self.config.get("logging.format", self.DEFAULT_FORMAT)
        datefmt = self.config.get("logging.date_format", self.DEFAULT_DATEFMT)
        formatter = logging.Formatter(fmt, datefmt=datefmt)

        today = datetime.now(self.config.timezone).strftime("%Y%m%d")
        log_file = self.logs_dir / f"{self.component_name}_{today}.log"

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        return logger

    def info(self, message: str) -> None:
        self.logger.info(message)

    def debug(self, message: str) -> None:
        self.logger.debug(message)

    def warning(self, message: str) -> None:
        self.logger.warning(message)

    def error(self, message: str, exc_info: bool = False) -> None:
        self.logger.error(message, exc_info=exc_info)

    def critical(self, message: str, exc_info: bool = False) -> None:
        self.logger.critical(message, exc_info=exc_info)


def get_module_logger(name: str) -> logging.Logger:
    """Lightweight stdout logger for standalone ETL scripts (no config needed)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(PipelineLogger.DEFAULT_FORMAT,
                              datefmt=PipelineLogger.DEFAULT_DATEFMT)
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ---------------------------------------------------------------------------
# Date helpers (timezone-aware; ERP stores dates as YYYYMMDD text — see rotacion)
# ---------------------------------------------------------------------------
def get_yesterday_yyyymmdd(tz: ZoneInfo) -> str:
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    return yesterday.strftime("%Y%m%d")


def get_month_range_yyyymmdd(tz: ZoneInfo) -> tuple[str, str]:
    """First-of-month .. yesterday, both YYYYMMDD (month-to-date validation)."""
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    first_day = yesterday.replace(day=1)
    return first_day.strftime("%Y%m%d"), yesterday.strftime("%Y%m%d")


def should_run_validation(tz: ZoneInfo, validation_weeks: list[int]) -> bool:
    """True if today's week-of-month (1-5) is in ``validation_weeks``."""
    day_of_month = datetime.now(tz).day
    week_of_month = ((day_of_month - 1) // 7) + 1
    return week_of_month in validation_weeks


def validate_yyyymmdd(value: str) -> str:
    """Accept 'YYYYMMDD' or 'YYYY-MM-DD'; return canonical 'YYYYMMDD'."""
    v = str(value).strip()
    if re.fullmatch(r"\d{8}", v):
        datetime.strptime(v, "%Y%m%d")  # validate calendar
        return v
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return datetime.strptime(v, "%Y-%m-%d").strftime("%Y%m%d")
    raise ValueError(f"Invalid date: {value!r}. Use YYYYMMDD or YYYY-MM-DD.")


def get_default_date_range_yyyymmdd(tz_name: str = "America/Bogota") -> tuple[str, str]:
    """Default target = yesterday (single-day range) in the given timezone."""
    y = get_yesterday_yyyymmdd(ZoneInfo(tz_name))
    return y, y


def parse_args_date_range(
    tz_name: str = "America/Bogota",
    extra_args: Optional[list] = None,
) -> argparse.Namespace:
    """Standard ETL CLI: ``--date`` OR (``--start-date`` and ``--end-date``),
    plus ``--dry-run``. Defaults to yesterday. Returns a Namespace with
    ``start_date``, ``end_date`` (canonical YYYYMMDD) and ``dry_run``.

    ``extra_args`` is a list of ``(args, kwargs)`` tuples for pipeline-specific
    flags, e.g. ``[(("--empresa",), {"help": "..."})]``.
    """
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--start-date", dest="start_date",
                        help="Range start (YYYYMMDD or YYYY-MM-DD)")
    parser.add_argument("--end-date", dest="end_date",
                        help="Range end (YYYYMMDD or YYYY-MM-DD)")
    parser.add_argument("--date", dest="single_date",
                        help="Shortcut for a single day (YYYYMMDD or YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate wiring without executing the source query")
    for a, kw in (extra_args or []):
        parser.add_argument(*a, **kw)
    args = parser.parse_args()

    if args.single_date and (args.start_date or args.end_date):
        raise ValueError("Use --date OR (--start-date/--end-date), not both.")

    if args.single_date:
        d = validate_yyyymmdd(args.single_date)
        args.start_date, args.end_date = d, d
        return args

    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise ValueError("For a range you must pass both --start-date and --end-date.")
        ini = validate_yyyymmdd(args.start_date)
        fin = validate_yyyymmdd(args.end_date)
        if ini > fin:
            raise ValueError(f"Invalid range: start ({ini}) > end ({fin}).")
        args.start_date, args.end_date = ini, fin
        return args

    args.start_date, args.end_date = get_default_date_range_yyyymmdd(tz_name)
    return args


# ---------------------------------------------------------------------------
# Log retention & formatting
# ---------------------------------------------------------------------------
def cleanup_old_logs(logs_dir: Path, retention_days: int,
                     logger: Optional[logging.Logger] = None) -> None:
    """Delete ``*.log`` and ``report_*.json`` older than ``retention_days``."""
    if not logs_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0
    for pattern in ("*.log", "report_*.json"):
        for f in logs_dir.glob(pattern):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
                    deleted += 1
                    if logger:
                        logger.debug(f"Deleted old file: {f.name}")
            except OSError as exc:
                if logger:
                    logger.warning(f"Error deleting {f.name}: {exc}")
    if logger and deleted:
        logger.info(f"Cleaned up {deleted} old log/report file(s)")


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


# ---------------------------------------------------------------------------
# JSON report helpers (factored out of the reference orchestrator)
# ---------------------------------------------------------------------------
def build_report(run_type: str, results: list, tz: ZoneInfo) -> dict:
    """Build the run report dict from a list of objects exposing ``to_dict()``,
    ``.success`` and ``.records_processed`` (see ``ETLResult``)."""
    return {
        "run_type": run_type,
        "timestamp": datetime.now(tz).isoformat(),
        "summary": {
            "total_etls": len(results),
            "successful": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
            "total_records": sum(r.records_processed for r in results),
        },
        "etls": [r.to_dict() for r in results],
    }


def write_json_report(logs_dir: Path, run_type: str, results: list,
                      tz: ZoneInfo) -> Path:
    """Write the run report to ``report_<runtype>_<timestamp>.json``; return path."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz).strftime("%Y%m%d_%H%M%S")
    report_file = logs_dir / f"report_{run_type.lower()}_{timestamp}.json"
    with open(report_file, "w", encoding="utf-8") as fh:
        json.dump(build_report(run_type, results, tz), fh, indent=2, ensure_ascii=False)
    return report_file
