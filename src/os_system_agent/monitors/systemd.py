"""Read ETL job status from systemd — the real Phase-1 freshness signal.

The monitored ETL jobs run as systemd services/timers, so "did it run, when,
and did it succeed" comes from ``systemctl show <unit>`` (read-only, allowlisted).
This module builds that command, parses its output, and turns it into a
:class:`~os_system_agent.monitors.freshness.JobStatus`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from os_system_agent.catalog import EtlJob
from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.severity import Severity, classify_freshness

# systemd prints timestamps like "Mon 2026-07-06 07:56:59 -05"; the weekday word
# is locale-dependent, so match the locale-independent date/time, and capture the
# offset separately (systemd uses 2-digit "-05", which %z does not accept as-is).
_DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*([+-]\d{2}(?::?\d{2})?)?")


def show_command(unit: str) -> str:
    """Return the read-only ``systemctl show`` command for ``unit``."""
    return f"systemctl show {unit} -p Result,ExecMainStatus,ExecMainExitTimestamp,ActiveState"


@dataclass(frozen=True)
class SystemdState:
    """Parsed last-run state of a systemd unit."""

    unit: str
    result: str
    exit_status: int | None
    last_exit_at: datetime | None
    active_state: str | None


def _parse_timestamp(value: str) -> datetime | None:
    """Parse a systemd timestamp into a tz-aware datetime, or None if absent."""
    match = _DT_RE.search(value)
    if not match:
        return None
    dt_part, offset = match.group(1), match.group(2)
    if not offset:  # naive would be unsafe to compare against tz-aware now
        return None
    offset = offset.replace(":", "")
    if len(offset) == 3:  # "-05" -> "-0500"
        offset += "00"
    try:
        return datetime.strptime(f"{dt_part} {offset}", "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def parse_state(text: str, unit: str) -> SystemdState:
    """Parse ``key=value`` output of ``systemctl show`` into a :class:`SystemdState`."""
    kv: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            kv[key.strip()] = val.strip()

    exit_raw = kv.get("ExecMainStatus", "").strip()
    try:
        exit_status: int | None = int(exit_raw) if exit_raw else None
    except ValueError:
        exit_status = None

    return SystemdState(
        unit=unit,
        result=kv.get("Result", "unknown") or "unknown",
        exit_status=exit_status,
        last_exit_at=_parse_timestamp(kv.get("ExecMainExitTimestamp", "")),
        active_state=kv.get("ActiveState") or None,
    )


def evaluate_systemd(job: EtlJob, state: SystemdState, now: datetime) -> JobStatus:
    """Turn a :class:`SystemdState` into a :class:`JobStatus` (fails closed).

    A non-``success`` result or non-zero exit is CRITICAL regardless of timing.
    Otherwise severity comes from how long ago the last successful run finished,
    using the job's freshness thresholds (set to the job's cadence).
    """
    if state.result != "success" or (state.exit_status not in (0, None)):
        return JobStatus(
            job_id=job.id,
            name=job.name,
            severity=Severity.CRITICAL,
            delay_minutes=None,
            latest_at=state.last_exit_at,
            evidence=f"{state.unit}: Result={state.result}, ExecMainStatus={state.exit_status}",
        )

    if state.last_exit_at is None:
        return JobStatus(
            job_id=job.id,
            name=job.name,
            severity=Severity.CRITICAL,
            delay_minutes=None,
            latest_at=None,
            evidence=f"{state.unit}: succeeded but no ExecMainExitTimestamp",
        )

    delay = (now - state.last_exit_at).total_seconds() / 60.0
    severity = classify_freshness(
        delay,
        job.freshness.warning_after_minutes,
        job.freshness.critical_after_minutes,
    )
    evidence = (
        f"{state.unit}: success, last run {state.last_exit_at.isoformat()} ({delay:.0f} min ago)"
    )
    return JobStatus(
        job_id=job.id,
        name=job.name,
        severity=severity,
        delay_minutes=delay,
        latest_at=state.last_exit_at,
        evidence=evidence,
    )
