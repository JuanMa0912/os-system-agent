from datetime import UTC, datetime, timedelta

from os_system_agent.catalog import EtlJob, FreshnessRule
from os_system_agent.monitors.systemd import evaluate_systemd, parse_state, show_command
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
    assert evaluate_systemd(DAILY, state, now).severity is Severity.CRITICAL
