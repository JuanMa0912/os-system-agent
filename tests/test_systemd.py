from dataclasses import replace
from datetime import UTC, datetime, timedelta

from os_system_agent.catalog import EtlJob, FreshnessRule
from os_system_agent.monitors.systemd import (
    evaluate_systemd,
    parse_multi,
    parse_state,
    show_command,
    show_command_multi,
    timer_for,
)
from os_system_agent.severity import Severity
from os_system_agent.ssh_client import is_read_only

DAILY = EtlJob(
    id="etl_rotacion",
    name="ETL Rotacion",
    server="server232",
    schedule="daily 07:00",
    freshness=FreshnessRule(1500, 1560),  # ~25h / ~26h — a daily-cadence job
    systemd_unit="etl-rotacion.service",
)

SHOW_OK = (
    "Result=success\n"
    "ExecMainStatus=0\n"
    "ExecMainExitTimestamp=Mon 2026-07-06 07:56:59 -05\n"
    "ActiveState=inactive\n"
)
SHOW_FAIL = (
    "Result=exit-code\n"
    "ExecMainStatus=1\n"
    "ExecMainExitTimestamp=Mon 2026-07-06 07:56:59 -05\n"
    "ActiveState=failed\n"
)
SHOW_NEVER = "Result=success\nExecMainStatus=0\nExecMainExitTimestamp=\nActiveState=inactive\n"
# Same empty-timestamp shape but the unit is currently active (mid-run).
SHOW_RUNNING = "Result=success\nExecMainStatus=0\nExecMainExitTimestamp=\nActiveState=active\n"
# A unit with SuccessExitStatus=3: exit 3 is success, so Result stays "success".
SHOW_OK_EXIT3 = (
    "Result=success\n"
    "ExecMainStatus=3\n"
    "ExecMainExitTimestamp=Mon 2026-07-06 07:56:59 -05\n"
    "ActiveState=inactive\n"
)


def test_show_command_passes_the_read_only_allowlist():
    assert is_read_only(show_command("etl-rotacion.service")) is True


def test_parse_state_reads_fields():
    state = parse_state(SHOW_OK, "etl-rotacion.service")
    assert state.result == "success"
    assert state.exit_status == 0
    assert state.last_exit_at is not None
    assert state.last_exit_at.hour == 7


def test_two_digit_offset_parses():
    state = parse_state(SHOW_OK, "x")
    assert state.last_exit_at is not None
    assert state.last_exit_at.utcoffset() == timedelta(hours=-5)


def test_success_recent_is_info():
    state = parse_state(SHOW_OK, "etl-rotacion.service")
    now = state.last_exit_at + timedelta(minutes=30)
    assert evaluate_systemd(DAILY, state, now).severity is Severity.INFO


def test_success_but_stale_is_critical():
    state = parse_state(SHOW_OK, "etl-rotacion.service")
    now = state.last_exit_at + timedelta(minutes=1600)  # missed a day
    assert evaluate_systemd(DAILY, state, now).severity is Severity.CRITICAL


def test_success_result_with_nonzero_exit_is_info():
    # systemd's Result is the success authority (unit may set SuccessExitStatus).
    state = parse_state(SHOW_OK_EXIT3, "visor-etl-sync.service")
    assert state.exit_status == 3
    now = state.last_exit_at + timedelta(minutes=30)
    assert evaluate_systemd(DAILY, state, now).severity is Severity.INFO


def test_failed_result_is_critical():
    state = parse_state(SHOW_FAIL, "etl-rotacion.service")
    now = state.last_exit_at + timedelta(minutes=5)
    status = evaluate_systemd(DAILY, state, now)
    assert status.severity is Severity.CRITICAL
    assert "exit-code" in status.evidence


def test_missing_timestamp_fails_closed():
    state = parse_state(SHOW_NEVER, "etl-rotacion.service")
    assert state.last_exit_at is None
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    status = evaluate_systemd(DAILY, state, now)
    assert status.severity is Severity.CRITICAL
    # Evidence must read as "never ran", not a misleading "succeeded".
    assert "never ran" in status.evidence
    assert "succeeded" not in status.evidence


def test_active_without_exit_timestamp_reports_running_as_info():
    """Mid-run is NOT a failure: systemd just cleared ExecMainExitTimestamp.

    Reporting CRITICAL here caused a real false alarm on 2026-08-09, when the
    check caught visor-etl-reconcile during its normal 32-minute run.
    """
    state = parse_state(SHOW_RUNNING, "etl-rotacion.service")
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    status = evaluate_systemd(DAILY, state, now)
    assert status.severity is Severity.INFO
    assert "en ejecución" in status.evidence


def test_timer_for_maps_services_including_templates():
    assert timer_for("visor-etl-reconcile.service") == "visor-etl-reconcile.timer"
    assert timer_for("etl-rotacion@mercamio.service") == "etl-rotacion@mercamio.timer"
    assert timer_for("visor-etl-reconcile.timer") is None
    assert timer_for("visor-etl-reconcile") is None


def test_show_command_multi_with_timers_passes_the_read_only_allowlist():
    command = show_command_multi(["a.service", "a.timer", "etl-rotacion@mercamio.timer"])
    assert is_read_only(command) is True
    assert command.endswith(",LastTriggerUSec")


def test_parse_multi_reads_last_trigger_only_from_timer_blocks():
    states = parse_multi(
        "Id=a.service\nResult=success\nExecMainExitTimestamp=\nActiveState=inactive\n"
        "\n"
        "Id=a.timer\nResult=success\nLastTriggerUSec=Sun 2026-10-18 16:04:47 -05\n"
        "ActiveState=active\n"
    )
    assert states["a.service"].last_trigger_at is None
    trigger = states["a.timer"].last_trigger_at
    assert trigger is not None
    assert trigger.utcoffset() == timedelta(hours=-5)


def test_reboot_lost_result_but_timer_fired_is_warning_not_never_ran():
    """systemd forgets ExecMainExitTimestamp on reboot; the timer remembers.

    On 2026-10-20 server232 rebooted and the weekend jobs read "never ran"
    (CRITICAL) for days. The outcome is unknown: WARNING, never INFO.
    """
    trigger = datetime(2026, 7, 5, 12, 9, tzinfo=UTC)
    state = replace(parse_state(SHOW_NEVER, "etl-rotacion.service"), last_trigger_at=trigger)
    now = trigger + timedelta(minutes=30)
    status = evaluate_systemd(DAILY, state, now)
    assert status.severity is Severity.WARNING
    assert status.latest_at == trigger
    assert status.delay_minutes == 30
    assert "timer disparó" in status.evidence
    assert "never ran" not in status.evidence


def test_reboot_lost_result_and_timer_stale_is_critical():
    # The timer itself stopped firing: freshness from the trigger still escalates.
    trigger = datetime(2026, 7, 5, 12, 9, tzinfo=UTC)
    state = replace(parse_state(SHOW_NEVER, "etl-rotacion.service"), last_trigger_at=trigger)
    now = trigger + timedelta(minutes=1600)  # missed a day for a daily job
    assert evaluate_systemd(DAILY, state, now).severity is Severity.CRITICAL
