import pytest

from os_system_agent.ssh_client import (
    UnsafeCommandError,
    assert_read_only,
    is_read_only,
    run_read_only,
    run_read_only_local,
)

READ_ONLY = [
    "hostname",
    "date",
    "uptime",
    "df -h",
    "systemctl status my-service --no-pager",
    "journalctl -u my-service --since '2 hours ago'",
    "ls -lah /opt/etl/output",
]

UNSAFE = [
    "",
    "rm -rf /",
    "sudo systemctl restart my-service",
    "systemctl restart my-service",
    "find /opt/etl -type f -delete",
    "cat secrets.env; rm notes.txt",
    "df -h && rm file",
    "psql -c 'drop table daily_sales'",
    "echo hi > /etc/passwd",
]


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only_commands_pass(cmd):
    assert is_read_only(cmd) is True


@pytest.mark.parametrize("cmd", UNSAFE)
def test_unsafe_commands_rejected(cmd):
    assert is_read_only(cmd) is False


def test_assert_raises_on_unsafe():
    with pytest.raises(UnsafeCommandError):
        assert_read_only("rm -rf /")


# --- local runner (co-located deployment, spec 004 T3) ---------------------

def test_local_runner_rejects_unsafe_before_executing():
    # Fails closed: the allowlist is enforced before any subprocess runs.
    with pytest.raises(UnsafeCommandError):
        run_read_only_local("rm -rf /")


def test_local_runner_executes_allowlisted_command():
    # `hostname` is allowlisted and exists on both Linux (target) and the dev box.
    result = run_read_only_local("hostname")
    assert result.exit_code == 0
    assert result.stdout.strip() != ""


def test_run_read_only_dispatches_local_alias_without_ssh():
    # alias "local" runs here, not over SSH — same successful result as the
    # local runner (no ssh binary involved).
    result = run_read_only("local", "hostname")
    assert result.exit_code == 0
    assert result.stdout.strip() != ""
