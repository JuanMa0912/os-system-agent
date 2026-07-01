import pytest

from os_system_agent.ssh_client import (
    UnsafeCommandError,
    assert_read_only,
    is_read_only,
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
