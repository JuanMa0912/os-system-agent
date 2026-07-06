#!/usr/bin/env python3
"""send_daily_report.py — build the daily ETL report and push it to a channel.

Designed to run from a systemd timer on the OpenClaw gateway host. It collects
ETL status (live over read-only SSH, or a dry-run mock), renders the daily
report, and delivers it via ``openclaw message send``.

Safety defaults (CLAUDE.md §14 — dry-run first, fail closed):

* Without ``--send`` it only prints the report — no message is delivered.
* Without a ``--target`` (or ``OS_TELEGRAM_TARGET``) it refuses to send.
* The recipient id and channel are never hard-coded here; they come from the
  timer/env on the gateway host so nothing sensitive is committed.

Flags:

* ``--live``            collect real status over SSH (default: dry-run mock).
* ``--send``            actually deliver (default: print only).
* ``--only-incidents``  deliver only when something is WARNING or worse — for a
  more frequent "alert-only" timer that stays quiet on healthy days.
"""

from __future__ import annotations

import argparse
import functools
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from os_system_agent.catalog import load_catalog
from os_system_agent.collector import build_daily_report, collect_statuses
from os_system_agent.reports.daily import overall_severity
from os_system_agent.severity import Severity

DEFAULT_CATALOG = Path("config/alert-rules.example.yml")
DEFAULT_SERVER_ALIAS = "server232"
DEFAULT_CHANNEL = "telegram"

# Telegram caps a message at 4096 chars; keep headroom for a chunk prefix.
CHUNK_LIMIT = 3900

# Severities that count as "something to alert about" for --only-incidents.
INCIDENT_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.WARNING, Severity.CRITICAL, Severity.SECURITY}
)

# A message-send callable: (channel, target, message) -> None. Injected in tests
# so the send path is exercised without invoking the real openclaw CLI.
Sender = Callable[[str, str, str], None]


def should_send(worst: Severity, *, only_incidents: bool) -> bool:
    """Whether to deliver, given the worst severity and the --only-incidents gate."""
    if not only_incidents:
        return True
    return worst in INCIDENT_SEVERITIES


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split ``text`` into ``<=limit`` chunks on line boundaries.

    Joining the result with newlines reconstructs the original text. A single
    line longer than ``limit`` is hard-sliced so no chunk ever exceeds the cap.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        while len(line) > limit:  # pathological long line: hard-slice it
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if length + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_via_openclaw(
    *,
    channel: str,
    target: str,
    message: str,
    binary: str = "openclaw",
    timeout: float = 30.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Deliver one message through ``openclaw message send`` (no shell)."""
    cmd = [
        binary,
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        message,
    ]
    proc = run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"openclaw message send failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )


def _default_sender(channel: str, target: str, message: str, *, binary: str) -> None:
    """The real sender: deliver through the openclaw CLI ``binary``."""
    send_via_openclaw(channel=channel, target=target, message=message, binary=binary)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the daily ETL report and push it.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--server-alias", default=DEFAULT_SERVER_ALIAS)
    parser.add_argument("--live", action="store_true", help="collect real status over SSH")
    parser.add_argument("--send", action="store_true", help="actually deliver (default: print)")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument(
        "--target",
        default=None,
        help="channel recipient id (falls back to OS_TELEGRAM_TARGET)",
    )
    parser.add_argument(
        "--openclaw-bin",
        default=None,
        help="path to the openclaw CLI (falls back to OPENCLAW_BIN, then 'openclaw')",
    )
    parser.add_argument(
        "--only-incidents",
        action="store_true",
        help="deliver only when something is WARNING or worse",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, sender: Sender | None = None) -> int:
    args = _parse_args(argv)
    jobs = load_catalog(args.catalog)
    now = datetime.now(UTC)

    statuses = collect_statuses(jobs, live=args.live, alias=args.server_alias, now=now)
    report = build_daily_report(jobs, statuses, now)
    worst = overall_severity(statuses)

    if not should_send(worst, only_incidents=args.only_incidents):
        print(f"[send_daily_report] overall={worst.value}; nothing to alert (--only-incidents).")
        return 0

    chunks = split_message(report)

    if not args.send:
        print(report)
        print(
            f"\n[dry-run] would send {len(chunks)} message(s) to "
            f"{args.channel} (pass --send to deliver).",
            file=sys.stderr,
        )
        return 0

    target = args.target or os.environ.get("OS_TELEGRAM_TARGET")
    if not target:
        print(
            "[send_daily_report] no --target and OS_TELEGRAM_TARGET is unset; refusing to send.",
            file=sys.stderr,
        )
        return 2  # fail closed

    if sender is not None:
        active_sender: Sender = sender
    else:
        binary = args.openclaw_bin or os.environ.get("OPENCLAW_BIN", "openclaw")
        active_sender = functools.partial(_default_sender, binary=binary)

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        prefix = f"({index}/{total})\n" if total > 1 else ""
        active_sender(args.channel, target, prefix + chunk)

    print(f"[send_daily_report] sent {total} message(s) to {args.channel}, overall={worst.value}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
