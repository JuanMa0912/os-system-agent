"""Human-readable ETL reports (CLAUDE.md §13).

Renders the daily ETL report and incident alerts. All job-derived evidence is
passed through :func:`os_system_agent.redaction.redact` before it reaches any
human-facing text — principle #4: no secrets in reports.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.redaction import redact
from os_system_agent.severity import Severity

# Highest-wins ordering for the overall status roll-up (CLAUDE.md §12).
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
    Severity.SECURITY: 3,
}


def overall_severity(statuses: Sequence[JobStatus]) -> Severity:
    """Return the worst severity across ``statuses`` (empty -> INFO).

    Ordering: SECURITY > CRITICAL > WARNING > INFO.
    """
    worst = Severity.INFO
    for status in statuses:
        if _SEVERITY_RANK[status.severity] > _SEVERITY_RANK[worst]:
            worst = status.severity
    return worst


def _delay_text(status: JobStatus) -> str:
    if status.delay_minutes is None:
        return "unknown"
    return f"{status.delay_minutes:.0f} min behind"


def render_daily_report(
    *,
    server: str,
    report_date: date,
    statuses: Sequence[JobStatus],
) -> str:
    """Render the daily ETL report (CLAUDE.md §13)."""
    lines: list[str] = [
        "OS_SYSTEM_AGENT — Daily ETL Report",
        "",
        f"Date: {report_date.isoformat()}",
        f"Server: {redact(server)}",
        f"Overall status: {overall_severity(statuses).value}",
        "",
        "Jobs:",
    ]

    if not statuses:
        lines.append("  (no jobs evaluated)")
    else:
        for number, status in enumerate(statuses, start=1):
            latest = status.latest_at.isoformat() if status.latest_at else "unknown"
            lines.extend(
                [
                    f"{number}. {redact(status.name)}",
                    "   Expected: per schedule",
                    "   Started: n/a (freshness check)",
                    f"   Finished: {latest}",
                    "   Duration: n/a",
                    f"   Freshness: {_delay_text(status)}",
                    "   Records/files: n/a",
                    f"   Status: {status.severity.value}",
                    f"   Evidence: {redact(status.evidence)}",
                ]
            )

    incidents = [s for s in statuses if s.severity in (Severity.CRITICAL, Severity.SECURITY)]
    warnings = [s for s in statuses if s.severity is Severity.WARNING]

    lines.extend(["", "Incidents:"])
    if incidents:
        for status in incidents:
            lines.append(
                f"- [{status.severity.value}] {redact(status.name)}: {redact(status.evidence)}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Recommended actions:"])
    if incidents:
        for status in incidents:
            lines.append(f"- Investigate {redact(status.name)} (freshness {_delay_text(status)})")
    elif warnings:
        for status in warnings:
            lines.append(f"- Watch {redact(status.name)} (within recovery window)")
    else:
        lines.append("- none")

    lines.extend(["", "Human approvals needed:"])
    if incidents:
        for status in incidents:
            lines.append(f"- Approval to rerun {redact(status.name)} (if confirmed failed)")
    else:
        lines.append("- none")

    return "\n".join(lines)


# Short severity tags for the compact chat format (token-cheap, plain ASCII).
_CHAT_TAG: dict[Severity, str] = {
    Severity.INFO: "OK",
    Severity.WARNING: "WARN",
    Severity.CRITICAL: "CRIT",
    Severity.SECURITY: "SEC",
}


def _humanize_minutes(minutes: float | None) -> str:
    """Render a delay in minutes as a short human string (e.g. ``6h 53m``)."""
    if minutes is None:
        return "sin dato"
    total = int(round(minutes))
    if total < 60:
        return f"{total}m"
    hours, mins = divmod(total, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h" if hrs else f"{days}d"


def render_chat_report(
    *,
    server: str,
    report_date: date,
    statuses: Sequence[JobStatus],
) -> str:
    """Render a compact, chat-friendly report: one line per job, no filler.

    Optimized for Telegram (no markdown tables — they don't render there) and for
    token cost (the daily §13 report is verbose). Evidence still goes through
    :func:`redact`.
    """
    worst = overall_severity(statuses)
    incidents = [s for s in statuses if s.severity in (Severity.CRITICAL, Severity.SECURITY)]
    warnings = [s for s in statuses if s.severity is Severity.WARNING]

    lines = [
        f"OS_SYSTEM_AGENT · ETL {report_date.isoformat()} · {redact(server)}",
        f"Estado: {worst.value} · incidentes: {len(incidents)} · avisos: {len(warnings)}",
        "",
    ]
    if not statuses:
        lines.append("(sin jobs evaluados)")
    for status in statuses:
        tag = _CHAT_TAG.get(status.severity, "?")
        when = status.latest_at.strftime("%m-%d %H:%M") if status.latest_at else "—"
        age = _humanize_minutes(status.delay_minutes)
        lines.append(f"{tag} · {redact(status.name)} — hace {age} ({when})")

    detail = incidents + warnings
    if detail:
        lines.append("")
        for status in detail:
            lines.append(f"! {redact(status.name)}: {redact(status.evidence)}")

    return "\n".join(lines)


def render_incident_alert(*, server: str, status: JobStatus, trace_id: str) -> str:
    """Render a single incident alert (CLAUDE.md §13).

    A CRITICAL or SECURITY status requires human approval before any rerun.
    """
    requires_approval = "yes" if status.severity in (Severity.CRITICAL, Severity.SECURITY) else "no"
    lines = [
        f"[{status.severity.value}] OS_SYSTEM_AGENT",
        "",
        f"Server: {redact(server)}",
        f"Job: {redact(status.name)}",
        f"Problem: freshness {status.severity.value} ({_delay_text(status)})",
        f"Evidence: {redact(status.evidence)}",
        "Impact: downstream reports may be stale or missing",
        "Suggested next step: confirm job status, then request approval to rerun",
        f"Requires approval: {requires_approval}",
        f"Trace ID: {trace_id}",
    ]
    return "\n".join(lines)
