from datetime import UTC, datetime, timedelta

from os_system_agent.catalog import EtlJob, FreshnessRule
from os_system_agent.monitors.freshness import JobStatus, evaluate_freshness
from os_system_agent.severity import Severity

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _job() -> EtlJob:
    return EtlJob(
        id="daily_sales",
        name="Daily Sales Load",
        server="server232",
        schedule="daily",
        freshness=FreshnessRule(warning_after_minutes=60, critical_after_minutes=120),
    )


def test_fresh_data_is_info():
    status = evaluate_freshness(_job(), NOW - timedelta(minutes=10), NOW)
    assert isinstance(status, JobStatus)
    assert status.severity is Severity.INFO
    assert status.delay_minutes == 10
    assert "min ago" in status.evidence


def test_late_but_within_recovery_is_warning():
    status = evaluate_freshness(_job(), NOW - timedelta(minutes=90), NOW)
    assert status.severity is Severity.WARNING
    assert status.delay_minutes == 90


def test_very_late_is_critical():
    status = evaluate_freshness(_job(), NOW - timedelta(minutes=200), NOW)
    assert status.severity is Severity.CRITICAL


def test_no_timestamp_fails_closed_to_critical():
    status = evaluate_freshness(_job(), None, NOW)
    assert status.severity is Severity.CRITICAL
    assert status.delay_minutes is None
    assert status.latest_at is None
    assert status.evidence == "no fresh timestamp found"
