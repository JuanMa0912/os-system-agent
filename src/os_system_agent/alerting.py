"""Proactive incident alerting — alert only when the incident set changes.

The daily push is a digest; this is the "something broke" signal. To avoid alert
fatigue it is **stateful**: given the previously-alerted incidents and the
current ones, it emits an alert only for *new or escalated* incidents and for
*recoveries*. When nothing changed, it stays silent.

Pure logic only (no I/O, no clock): the caller loads/saves the state file and
sends the message. This keeps the diff deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.redaction import redact
from os_system_agent.reports.daily import CHAT_TAG
from os_system_agent.severity import Severity

# Severities that count as an incident worth an out-of-band alert.
ALERT_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.WARNING, Severity.CRITICAL, Severity.SECURITY}
)

# Synthetic job id used when the ETL server itself is unreachable.
SERVER_DOWN_ID = "__server__"


def server_down_status(server: str) -> JobStatus:
    """A synthetic CRITICAL status representing an unreachable ETL server."""
    return JobStatus(
        job_id=SERVER_DOWN_ID,
        name="Servidor ETL",
        severity=Severity.CRITICAL,
        delay_minutes=None,
        latest_at=None,
        evidence=f"{server}: sin respuesta por SSH",
    )


def incident_statuses(statuses: list[JobStatus]) -> list[JobStatus]:
    """Keep only statuses that are WARNING or worse."""
    return [s for s in statuses if s.severity in ALERT_SEVERITIES]


@dataclass(frozen=True)
class AlertOutcome:
    """Result of diffing current incidents against the previously-alerted set."""

    to_alert: list[JobStatus] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    state: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        """True when there is something new to alert or a recovery to report."""
        return bool(self.to_alert or self.recovered)


def diff_incidents(
    previous: dict[str, str],
    current_incidents: list[JobStatus],
) -> AlertOutcome:
    """Diff current incidents against the last-alerted state.

    ``previous`` maps ``job_id -> severity value`` from the last alert. An
    incident is alerted when it is new or its severity changed (e.g. WARNING ->
    CRITICAL). A job present in ``previous`` but no longer an incident recovered.
    """
    current_state = {s.job_id: s.severity.value for s in current_incidents}
    to_alert = [s for s in current_incidents if previous.get(s.job_id) != s.severity.value]
    recovered = [job_id for job_id in previous if job_id not in current_state]
    return AlertOutcome(to_alert=to_alert, recovered=recovered, state=current_state)


def render_alert(
    *,
    server: str,
    report_date: date,
    to_alert: list[JobStatus],
    recovered: list[str],
    names: dict[str, str] | None = None,
    empresa: str = "unknown",
) -> str:
    """Render a compact alert message for changed incidents and recoveries.

    ``names`` optionally maps a recovered ``job_id`` to a human name; ids fall
    back to themselves when unmapped. The header names the ``empresa`` so a
    CRITICAL landing in a shared Telegram group is unambiguous about which
    company it is about.
    """
    names = names or {}
    lines = [
        f"⚠️ OS_SYSTEM_AGENT · empresa {redact(empresa)} — "
        f"cambios ETL {report_date.isoformat()} · {redact(server)}"
    ]

    if to_alert:
        lines.append("")
        lines.append("Incidentes:")
        for status in to_alert:
            tag = CHAT_TAG.get(status.severity, "?")
            lines.append(f"{tag} · {redact(status.name)}: {redact(status.evidence)}")

    if recovered:
        lines.append("")
        lines.append("Recuperados:")
        for job_id in recovered:
            lines.append(f"OK · {redact(names.get(job_id, job_id))}")

    return "\n".join(lines)
