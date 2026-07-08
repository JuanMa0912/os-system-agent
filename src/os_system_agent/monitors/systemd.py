"""Read ETL job status from systemd — the real Phase-1 freshness signal.

The monitored ETL jobs run as systemd services/timers, so "did it run, when,
and did it succeed" comes from ``systemctl show <unit>`` (read-only, allowlisted).
This module builds that command, parses its output, and turns it into a
:class:`~os_system_agent.monitors.freshness.JobStatus`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from os_system_agent.catalog import EtlJob
from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.severity import Severity, classify_freshness

# systemd prints timestamps like "Mon 2026-07-06 07:56:59 -05"; the weekday word
# is locale-dependent, so match the locale-independent date/time, and capture the
# offset separately (systemd uses 2-digit "-05", which %z does not accept as-is).
_DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*([+-]\d{2}(?::?\d{2})?)?")


# Properties read per unit. ``Id`` is included in the multi form so each block
# can be keyed back to its unit regardless of ordering.
_PROPS = "Result,ExecMainStatus,ExecMainExitTimestamp,ActiveState"
_PROPS_MULTI = f"Id,{_PROPS}"


def show_command(unit: str) -> str:
    """Return the read-only ``systemctl show`` command for a single ``unit``."""
    return f"systemctl show {unit} -p {_PROPS}"


def show_command_multi(units: Sequence[str]) -> str:
    """Return ONE read-only ``systemctl show`` command for many units.

    ``systemctl show`` accepts multiple units and prints one blank-line-separated
    block each; ``Id`` keys every block. This collapses N SSH round-trips to 1.
    """
    return f"systemctl show {' '.join(units)} -p {_PROPS_MULTI}"


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


def _kv_from_block(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines of one ``systemctl show`` block."""
    kv: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            kv[key.strip()] = val.strip()
    return kv


def _state_from_kv(kv: dict[str, str], unit: str) -> SystemdState:
    """Build a :class:`SystemdState` from parsed ``key=value`` properties."""
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


def parse_state(text: str, unit: str) -> SystemdState:
    """Parse single-unit ``systemctl show`` output into a :class:`SystemdState`."""
    return _state_from_kv(_kv_from_block(text), unit)


def parse_multi(text: str) -> dict[str, SystemdState]:
    """Parse multi-unit ``systemctl show`` output into ``{unit_id: SystemdState}``.

    Blocks are separated by a blank line and each is keyed by its ``Id``.
    """
    states: dict[str, SystemdState] = {}
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        if not block.strip():
            continue
        kv = _kv_from_block(block)
        unit = kv.get("Id") or ""
        if unit:
            states[unit] = _state_from_kv(kv, unit)
    return states


def evaluate_systemd(job: EtlJob, state: SystemdState, now: datetime) -> JobStatus:
    """Turn a :class:`SystemdState` into a :class:`JobStatus` (fails closed).

    systemd's ``Result`` is the success authority: a unit may legitimately set
    ``SuccessExitStatus`` so a non-zero ``ExecMainStatus`` still yields
    ``Result=success``. So a non-``success`` result is CRITICAL regardless of
    timing; ``ExecMainStatus`` is kept only as evidence. Otherwise severity comes
    from how long ago the last successful run finished (freshness thresholds).
    """
    if state.result != "success":
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
