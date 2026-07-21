"""ETL job catalog loading and validation (FR-003, CLAUDE.md §11, §14).

Loads the human-authored alert-rules YAML into typed, immutable job records.
Fails closed: any missing, malformed, or inconsistent entry raises
:class:`CatalogError` rather than yielding a silently-degraded catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class CatalogError(RuntimeError):
    """Raised when the ETL job catalog is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class FreshnessRule:
    """Per-job freshness thresholds, in minutes of delay."""

    warning_after_minutes: float
    critical_after_minutes: float


@dataclass(frozen=True)
class EtlJob:
    """A single monitored ETL job (immutable, validated)."""

    id: str
    name: str
    server: str
    schedule: str
    freshness: FreshnessRule
    # Company this job belongs to. A catalog is single-empresa, so this is
    # denormalized from the catalog's top-level ``empresa`` by ``load_catalog``.
    # The default only serves direct construction (tests/tools); the loader
    # always sets it explicitly and fails closed if the catalog omits it.
    empresa: str = "unknown"
    systemd_unit: str | None = None
    expected_finish_before: str | None = None
    log_path: str | None = None
    output_path: str | None = None
    alert_telegram: bool = True


def _require_number(value: Any, *, job_id: str, field: str) -> float:
    """Coerce ``value`` to float or fail closed with a clear message."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"job {job_id!r}: freshness.{field} must be a number, got {value!r}")
    return float(value)


def _parse_job(raw: Any, *, index: int, empresa: str) -> EtlJob:
    """Validate and build one :class:`EtlJob` from a raw mapping."""
    if not isinstance(raw, dict):
        raise CatalogError(f"jobs[{index}] must be a mapping, got {type(raw).__name__}")

    job_id = raw.get("id")
    if not job_id or not isinstance(job_id, str):
        raise CatalogError(f"jobs[{index}] is missing a non-empty string 'id'")

    server = raw.get("server")
    if not server or not isinstance(server, str):
        raise CatalogError(f"job {job_id!r} is missing a non-empty string 'server'")

    freshness_raw = raw.get("freshness")
    if not isinstance(freshness_raw, dict):
        raise CatalogError(f"job {job_id!r} is missing a 'freshness' mapping")

    warning = _require_number(
        freshness_raw.get("max_delay_minutes_warning"),
        job_id=job_id,
        field="max_delay_minutes_warning",
    )
    critical = _require_number(
        freshness_raw.get("max_delay_minutes_critical"),
        job_id=job_id,
        field="max_delay_minutes_critical",
    )
    if warning > critical:
        raise CatalogError(
            f"job {job_id!r}: warning threshold ({warning}) must not exceed "
            f"critical threshold ({critical})"
        )

    paths = raw.get("paths") or {}
    if not isinstance(paths, dict):
        raise CatalogError(f"job {job_id!r}: 'paths' must be a mapping if present")
    alerts = raw.get("alerts") or {}
    if not isinstance(alerts, dict):
        raise CatalogError(f"job {job_id!r}: 'alerts' must be a mapping if present")

    return EtlJob(
        id=job_id,
        name=str(raw.get("name") or job_id),
        server=server,
        schedule=str(raw.get("schedule") or "unspecified"),
        empresa=empresa,
        freshness=FreshnessRule(
            warning_after_minutes=warning,
            critical_after_minutes=critical,
        ),
        systemd_unit=(str(raw["systemd_unit"]) if raw.get("systemd_unit") is not None else None),
        expected_finish_before=(
            str(raw["expected_finish_before"])
            if raw.get("expected_finish_before") is not None
            else None
        ),
        log_path=str(paths["logs"]) if paths.get("logs") is not None else None,
        output_path=str(paths["output"]) if paths.get("output") is not None else None,
        alert_telegram=bool(alerts.get("telegram", True)),
    )


def load_catalog(path: Path) -> list[EtlJob]:
    """Load and validate the ETL job catalog from ``path``.

    Fails closed on: missing file, invalid YAML, no top-level ``jobs`` list,
    an empty jobs list, a missing top-level ``empresa``, jobs missing
    ``id``/``server``, non-numeric freshness thresholds, warning>critical, or
    duplicate ids.
    """
    path = Path(path)
    if not path.is_file():
        raise CatalogError(f"catalog file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"could not read/parse catalog {path}: {exc}") from exc

    if not isinstance(data, dict) or "jobs" not in data:
        raise CatalogError(f"catalog {path} must contain a top-level 'jobs' list")

    jobs_raw = data["jobs"]
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise CatalogError(f"catalog {path} has an empty or invalid 'jobs' list")

    # Every catalog belongs to exactly one empresa; the report/alert label depends
    # on it, so a missing empresa is a fail-closed error (never an unlabeled report).
    empresa = data.get("empresa")
    if not empresa or not isinstance(empresa, str):
        raise CatalogError(f"catalog {path} must set a top-level non-empty 'empresa'")

    jobs: list[EtlJob] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(jobs_raw):
        job = _parse_job(raw, index=index, empresa=empresa)
        if job.id in seen_ids:
            raise CatalogError(f"duplicate job id in catalog: {job.id!r}")
        seen_ids.add(job.id)
        jobs.append(job)

    return jobs
