"""Tests for proactive incident alerting (os_system_agent.alerting)."""

from __future__ import annotations

from datetime import date

from os_system_agent.alerting import (
    SERVER_DOWN_ID,
    diff_incidents,
    incident_statuses,
    render_alert,
    server_down_status,
)
from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.severity import Severity

TODAY = date(2026, 7, 6)


def _status(job_id: str, severity: Severity, evidence: str = "") -> JobStatus:
    return JobStatus(
        job_id=job_id,
        name=f"Job {job_id}",
        severity=severity,
        delay_minutes=None,
        latest_at=None,
        evidence=evidence or f"{job_id}.service",
    )


def test_incident_statuses_filters_warning_and_worse() -> None:
    statuses = [
        _status("a", Severity.INFO),
        _status("b", Severity.WARNING),
        _status("c", Severity.CRITICAL),
    ]
    assert [s.job_id for s in incident_statuses(statuses)] == ["b", "c"]


def test_diff_alerts_on_new_incident() -> None:
    outcome = diff_incidents({}, [_status("b", Severity.CRITICAL)])
    assert [s.job_id for s in outcome.to_alert] == ["b"]
    assert outcome.recovered == []
    assert outcome.state == {"b": "CRITICAL"}
    assert outcome.changed


def test_diff_stays_silent_when_unchanged() -> None:
    previous = {"b": "CRITICAL"}
    outcome = diff_incidents(previous, [_status("b", Severity.CRITICAL)])
    assert outcome.to_alert == []
    assert outcome.recovered == []
    assert not outcome.changed


def test_diff_alerts_on_escalation() -> None:
    previous = {"b": "WARNING"}
    outcome = diff_incidents(previous, [_status("b", Severity.CRITICAL)])
    assert [s.job_id for s in outcome.to_alert] == ["b"]
    assert outcome.changed


def test_diff_reports_recovery() -> None:
    previous = {"b": "CRITICAL"}
    outcome = diff_incidents(previous, [])
    assert outcome.to_alert == []
    assert outcome.recovered == ["b"]
    assert outcome.state == {}
    assert outcome.changed


def test_server_down_status_is_critical() -> None:
    status = server_down_status("server232")
    assert status.job_id == SERVER_DOWN_ID
    assert status.severity is Severity.CRITICAL
    assert "server232" in status.evidence


def test_render_alert_lists_incidents_and_recoveries() -> None:
    message = render_alert(
        server="server232",
        report_date=TODAY,
        to_alert=[_status("ventas", Severity.CRITICAL, "ventas.service: Result=failed")],
        recovered=["rotacion"],
        names={"rotacion": "Rotación diaria"},
    )
    assert "cambios ETL 2026-07-06 · server232" in message
    assert "CRIT · Job ventas: ventas.service: Result=failed" in message
    assert "Recuperados:" in message
    assert "OK · Rotación diaria" in message
    assert "|" not in message  # no markdown table


def test_render_alert_recovered_falls_back_to_id() -> None:
    message = render_alert(
        server="s", report_date=TODAY, to_alert=[], recovered=["some-job"], names=None
    )
    assert "OK · some-job" in message


def test_render_alert_names_empresa_in_header() -> None:
    message = render_alert(
        server="server232",
        report_date=TODAY,
        to_alert=[_status("ventas", Severity.CRITICAL)],
        recovered=[],
        empresa="Mercamio",
    )
    # The empresa is named in the header, and the existing header shape is kept.
    assert "empresa Mercamio" in message
    assert "cambios ETL 2026-07-06 · server232" in message
