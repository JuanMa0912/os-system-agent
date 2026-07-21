from datetime import UTC, date, datetime

from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.reports.daily import (
    overall_severity,
    render_daily_report,
    render_incident_alert,
)
from os_system_agent.severity import Severity

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
REPORT_DATE = date(2026, 7, 6)

# A fake secret embedded in evidence to verify redaction before human-facing text.
LEAKY_EVIDENCE = "load failed: postgres://u:s3cr3tPWD@h/db"


def _status(severity: Severity, *, evidence: str = "latest at 2026-07-06T11:30 (30 min ago)"):
    return JobStatus(
        job_id="daily_sales",
        name="Daily Sales Load",
        severity=severity,
        delay_minutes=30.0,
        latest_at=NOW,
        evidence=evidence,
    )


def test_daily_report_has_section_headers():
    report = render_daily_report(
        server="server232",
        report_date=REPORT_DATE,
        statuses=[_status(Severity.INFO)],
    )
    assert "OS_SYSTEM_AGENT — Daily ETL Report" in report
    assert "Date: 2026-07-06" in report
    assert "Server: server232" in report
    assert "Overall status:" in report
    assert "Jobs:" in report
    assert "Incidents:" in report
    assert "Recommended actions:" in report
    assert "Human approvals needed:" in report


def test_daily_report_redacts_secret_in_evidence():
    report = render_daily_report(
        server="server232",
        report_date=REPORT_DATE,
        statuses=[_status(Severity.CRITICAL, evidence=LEAKY_EVIDENCE)],
    )
    assert "s3cr3tPWD" not in report


def test_daily_report_shows_empresa():
    report = render_daily_report(
        empresa="Mercamio",
        server="server232",
        report_date=REPORT_DATE,
        statuses=[_status(Severity.INFO)],
    )
    assert "Empresa: Mercamio" in report


def test_overall_severity_picks_worst():
    statuses = [_status(Severity.INFO), _status(Severity.WARNING), _status(Severity.CRITICAL)]
    assert overall_severity(statuses) is Severity.CRITICAL


def test_overall_severity_security_beats_critical():
    statuses = [_status(Severity.CRITICAL), _status(Severity.SECURITY)]
    assert overall_severity(statuses) is Severity.SECURITY


def test_overall_severity_empty_is_info():
    assert overall_severity([]) is Severity.INFO


def test_incident_alert_critical_requires_approval():
    alert = render_incident_alert(
        server="server232",
        status=_status(Severity.CRITICAL),
        trace_id="trace-abc123",
    )
    assert "[CRITICAL] OS_SYSTEM_AGENT" in alert
    assert "Requires approval: yes" in alert
    assert "Trace ID: trace-abc123" in alert


def test_incident_alert_info_does_not_require_approval():
    alert = render_incident_alert(
        server="server232",
        status=_status(Severity.INFO),
        trace_id="trace-xyz",
    )
    assert "Requires approval: no" in alert


def test_incident_alert_redacts_secret_in_evidence():
    alert = render_incident_alert(
        server="server232",
        status=_status(Severity.CRITICAL, evidence=LEAKY_EVIDENCE),
        trace_id="trace-1",
    )
    assert "s3cr3tPWD" not in alert
