"""Tests for the shared collection/report pipeline (os_system_agent.collector)."""

from __future__ import annotations

from datetime import UTC, datetime

from os_system_agent.catalog import EtlJob, FreshnessRule
from os_system_agent.collector import (
    build_daily_report,
    collect_dry_run,
    collect_live,
    collect_statuses,
)
from os_system_agent.severity import Severity
from os_system_agent.ssh_client import CommandResult

NOW = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)

# systemctl show output for a job that succeeded ~3 minutes before NOW.
CANNED_SHOW = (
    "Id=test.service\n"
    "Result=success\n"
    "ExecMainStatus=0\n"
    "ExecMainExitTimestamp=Mon 2026-07-06 07:56:59 -05\n"  # 12:56:59 UTC
    "ActiveState=inactive\n"
)


def _job(job_id: str = "j", unit: str | None = None) -> EtlJob:
    return EtlJob(
        id=job_id,
        name=f"Job {job_id}",
        server="testserver",
        schedule="daily",
        freshness=FreshnessRule(warning_after_minutes=1500, critical_after_minutes=1560),
        systemd_unit=unit,
    )


def test_dry_run_marks_all_jobs_info() -> None:
    jobs = [_job("a"), _job("b")]
    statuses = collect_dry_run(jobs, NOW)
    assert [s.severity for s in statuses] == [Severity.INFO, Severity.INFO]
    assert [s.job_id for s in statuses] == ["a", "b"]


def test_collect_statuses_dry_matches_collect_dry_run() -> None:
    jobs = [_job("a")]
    assert collect_statuses(jobs, live=False, alias="x", now=NOW) == collect_dry_run(jobs, NOW)


def test_collect_live_uses_injected_runner() -> None:
    calls: list[str] = []

    def runner(alias: str, command: str) -> CommandResult:
        calls.append(command)
        assert alias == "server232"
        assert "test.service" in command
        return CommandResult(command=command, exit_code=0, stdout=CANNED_SHOW, stderr="")

    jobs = [_job("a", unit="test.service")]
    statuses = collect_live(jobs, "server232", NOW, runner=runner)

    assert len(statuses) == 1
    status = statuses[0]
    assert status.severity is Severity.INFO
    assert status.delay_minutes is not None and 0 <= status.delay_minutes < 10
    assert "test.service" in status.evidence
    assert calls == [
        "systemctl show test.service -p Id,Result,ExecMainStatus,"
        "ExecMainExitTimestamp,ActiveState"
    ]


def test_collect_live_batches_all_units_into_one_ssh_call() -> None:
    calls: list[str] = []
    multi_output = (
        "Id=a.service\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "ExecMainExitTimestamp=Mon 2026-07-06 07:56:59 -05\n"
        "ActiveState=inactive\n"
        "\n"
        "Id=b.service\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "ExecMainExitTimestamp=Mon 2026-07-06 12:50:00 -05\n"
        "ActiveState=inactive\n"
    )

    def runner(alias: str, command: str) -> CommandResult:
        calls.append(command)
        return CommandResult(command=command, exit_code=0, stdout=multi_output, stderr="")

    jobs = [_job("a", unit="a.service"), _job("b", unit="b.service")]
    statuses = collect_live(jobs, "server232", NOW, runner=runner)

    assert len(calls) == 1  # ONE SSH round-trip for both units
    assert calls[0] == (
        "systemctl show a.service b.service -p Id,Result,ExecMainStatus,"
        "ExecMainExitTimestamp,ActiveState"
    )
    assert [s.severity for s in statuses] == [Severity.INFO, Severity.INFO]
    assert "a.service" in statuses[0].evidence
    assert "b.service" in statuses[1].evidence


def test_collect_live_fails_closed_when_unit_missing_from_output() -> None:
    def runner(alias: str, command: str) -> CommandResult:
        return CommandResult(command=command, exit_code=0, stdout="Id=other.service\n", stderr="")

    jobs = [_job("a", unit="test.service")]
    statuses = collect_live(jobs, "server232", NOW, runner=runner)
    assert statuses[0].severity is Severity.CRITICAL


def test_collect_live_skips_jobs_without_a_unit() -> None:
    def runner(alias: str, command: str) -> CommandResult:  # pragma: no cover - must not run
        raise AssertionError("runner should not be called for unitless jobs")

    assert collect_live([_job("a", unit=None)], "server232", NOW, runner=runner) == []


def test_build_daily_report_contains_header_and_server() -> None:
    jobs = [_job("a")]
    statuses = collect_dry_run(jobs, NOW)
    report = build_daily_report(jobs, statuses, NOW)
    assert "OS_SYSTEM_AGENT — Daily ETL Report" in report
    assert "Server: testserver" in report
    assert "2026-07-06" in report
