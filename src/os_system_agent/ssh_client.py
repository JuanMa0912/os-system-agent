"""SSH command safety: read-only allowlist enforcement (CLAUDE.md §10, §17).

Phase 1 is monitoring-only. This module decides whether a command is a safe,
read-only check. It deliberately does NOT open SSH connections yet — actual
execution lands in a later phase behind the approval parser.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

# Aliases that mean "run here, not over SSH" — used by the co-located deployment
# (spec 004: one agent per empresa, running ON that empresa's ETL server).
LOCAL_ALIASES: frozenset[str] = frozenset({"local", "localhost"})

# Commands whose first token is allowed for autonomous read-only checks.
READ_ONLY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "hostname",
        "date",
        "uptime",
        "whoami",
        "id",
        "df",
        "free",
        "uname",
        "systemctl",
        "journalctl",
        "ls",
        "find",
        "stat",
        "cat",
        "tail",
        "head",
        "wc",
        "grep",
    }
)

# systemctl subcommands that stay read-only.
_SYSTEMCTL_READONLY: frozenset[str] = frozenset(
    {"status", "is-active", "is-enabled", "show", "list-units", "list-timers"}
)

# Tokens that indicate a mutating or dangerous operation anywhere in the command.
DESTRUCTIVE_TOKENS: frozenset[str] = frozenset(
    {
        "rm",
        "mv",
        "cp",
        "dd",
        "truncate",
        "shred",
        "mkfs",
        "chown",
        "chmod",
        "tee",
        "kill",
        "pkill",
        "reboot",
        "shutdown",
        "sudo",
        "su",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "pip",
        "npm",
        "-delete",
        "-exec",
        "drop",
        "delete",
        "update",
        "insert",
        "alter",
        "create",
        "vacuum",
        "reindex",
    }
)

# Shell metacharacters that could chain or hide a second command.
_UNSAFE_CHARS: frozenset[str] = frozenset({";", "&", "|", "`", "$", ">", "<", "\n"})


class UnsafeCommandError(RuntimeError):
    """Raised when a command is not a verified read-only check."""


def is_read_only(command: str) -> bool:
    """Return ``True`` only if ``command`` is a recognized safe read-only check."""
    cmd = command.strip()
    if not cmd:
        return False
    if any(ch in cmd for ch in _UNSAFE_CHARS):
        return False
    tokens = cmd.split()
    if {t.lower() for t in tokens} & DESTRUCTIVE_TOKENS:
        return False
    head = tokens[0].lower()
    if head not in READ_ONLY_ALLOWLIST:
        return False
    if head == "systemctl" and len(tokens) > 1 and tokens[1].lower() not in _SYSTEMCTL_READONLY:
        return False
    return True


def assert_read_only(command: str) -> None:
    """Raise :class:`UnsafeCommandError` if ``command`` is not read-only."""
    if not is_read_only(command):
        raise UnsafeCommandError(f"command is not an allowlisted read-only check: {command!r}")


@dataclass(frozen=True)
class CommandResult:
    """Captured result of a read-only SSH command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str


def run_read_only_local(command: str, *, timeout: float = 15.0) -> CommandResult:
    """Run an allowlisted read-only ``command`` **locally** (no SSH).

    For the co-located deployment the agent runs on the ETL server itself, so
    monitoring needs no SSH-to-self. Fails closed: :func:`assert_read_only` runs
    before execution, and the command is split with :func:`shlex.split` and run
    without a shell, so no shell metacharacter can chain a second command.
    """
    assert_read_only(command)
    proc = subprocess.run(
        shlex.split(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def run_read_only(alias: str, command: str, *, timeout: float = 15.0) -> CommandResult:
    """Run an allowlisted read-only ``command`` on the SSH ``alias``.

    If ``alias`` is a local alias (``local``/``localhost``) the command runs
    here instead of over SSH — see :func:`run_read_only_local`. Otherwise it is
    dispatched over SSH.

    Fails closed: :func:`assert_read_only` is enforced *before* any connection
    is attempted, so a non-allowlisted command never reaches the server.
    ``BatchMode=yes`` disables interactive prompts (no password/passphrase).
    """
    if alias.lower() in LOCAL_ALIASES:
        return run_read_only_local(command, timeout=timeout)

    assert_read_only(command)
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, command]
    proc = subprocess.run(
        ssh_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
