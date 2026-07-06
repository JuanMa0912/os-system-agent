"""Pure ETL freshness evaluation (CLAUDE.md §11 signal #4, §14).

No I/O, no clock reads: ``now`` and the observed ``latest_at`` are injected so
the evaluation is deterministic and unit-testable. Classification is delegated
to :func:`os_system_agent.severity.classify_freshness`, which fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from os_system_agent.catalog import EtlJob
from os_system_agent.severity import Severity, classify_freshness


@dataclass(frozen=True)
class JobStatus:
    """The evaluated freshness status of a single ETL job."""

    job_id: str
    name: str
    severity: Severity
    delay_minutes: float | None
    latest_at: datetime | None
    evidence: str


def evaluate_freshness(
    job: EtlJob,
    latest_at: datetime | None,
    now: datetime,
) -> JobStatus:
    """Evaluate how fresh ``job`` is, given its latest observed timestamp.

    ``latest_at`` is the most recent output/log timestamp we could observe, or
    ``None`` if none was found. ``now`` is the reference time (injected — this
    function never reads the clock). Delay is ``None`` when ``latest_at`` is
    ``None``, which :func:`classify_freshness` treats as CRITICAL (fail closed).
    """
    if latest_at is None:
        delay_minutes: float | None = None
        evidence = "no fresh timestamp found"
    else:
        delay_minutes = (now - latest_at).total_seconds() / 60.0
        evidence = f"latest at {latest_at.isoformat()} ({delay_minutes:.0f} min ago)"

    severity = classify_freshness(
        delay_minutes,
        job.freshness.warning_after_minutes,
        job.freshness.critical_after_minutes,
    )
    return JobStatus(
        job_id=job.id,
        name=job.name,
        severity=severity,
        delay_minutes=delay_minutes,
        latest_at=latest_at,
        evidence=evidence,
    )
