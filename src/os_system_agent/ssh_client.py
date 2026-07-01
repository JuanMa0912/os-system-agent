"""SSH command safety: read-only allowlist enforcement (CLAUDE.md §10, §17).

Phase 1 is monitoring-only. This module decides whether a command is a safe,
read-only check. It deliberately does NOT open SSH connections yet — actual
execution lands in a later phase behind the approval parser.
"""

from __future__ import annotations

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
