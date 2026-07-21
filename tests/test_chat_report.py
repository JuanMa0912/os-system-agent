"""Tests for the compact chat report renderer (reports.daily.render_chat_report)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.reports.daily import _humanize_minutes, render_chat_report
from os_system_agent.severity import Severity

WHEN = datetime(2026, 7, 6, 7, 56, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (None, "sin dato"),
        (0, "0m"),
        (44, "44m"),
        (60, "1h"),
        (413, "6h 53m"),
        (1440, "1d"),
        (1500, "1d 1h"),
        (7373, "5d 2h"),
    ],
)
def test_humanize_minutes(minutes: float | None, expected: str) -> None:
    assert _humanize_minutes(minutes) == expected


def _status(name: str, severity: Severity, delay: float | None) -> JobStatus:
    return JobStatus(
        job_id=name,
        name=name,
        severity=severity,
        delay_minutes=delay,
        latest_at=WHEN,
        evidence=f"{name}.service: success",
    )


def test_chat_report_one_line_per_job_and_no_table() -> None:
    statuses = [
        _status("Ventas diaria", Severity.INFO, 465),
        _status("Rotación", Severity.INFO, 413),
    ]
    report = render_chat_report(server="server232", report_date=WHEN.date(), statuses=statuses)

    assert "OS_SYSTEM_AGENT · ETL 2026-07-06 · server232" in report
    assert "Estado: INFO · incidentes: 0 · avisos: 0" in report
    assert "OK · Ventas diaria — hace 7h 45m (07-06 07:56)" in report
    assert "OK · Rotación — hace 6h 53m (07-06 07:56)" in report
    assert "|" not in report  # never a markdown table


def test_chat_report_leads_with_empresa() -> None:
    statuses = [_status("Ventas diaria", Severity.INFO, 465)]
    report = render_chat_report(
        empresa="Mercamio", server="server232", report_date=WHEN.date(), statuses=statuses
    )
    # The empresa label is the first line so the operator can tell, at a glance in
    # a shared Telegram group, which company a message is about.
    assert report.startswith("Reporte empresa Mercamio")
    # The existing machine-ish header line is preserved right below it.
    assert "OS_SYSTEM_AGENT · ETL 2026-07-06 · server232" in report


def test_chat_report_lists_incidents_and_counts() -> None:
    statuses = [
        _status("Ventas diaria", Severity.CRITICAL, 3000),
        _status("Rotación", Severity.WARNING, 1600),
        _status("Sync GCP", Severity.INFO, 120),
    ]
    report = render_chat_report(server="server232", report_date=WHEN.date(), statuses=statuses)

    assert "incidentes: 1 · avisos: 1" in report
    assert "CRIT · Ventas diaria" in report
    assert "! Ventas diaria: Ventas diaria.service: success" in report
    assert "! Rotación:" in report
