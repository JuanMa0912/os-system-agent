#!/usr/bin/env python3
"""send_daily_report.py — build the daily ETL report and push it to a channel.

Designed to run from a systemd timer on the OpenClaw gateway host. It collects
ETL status (live over read-only SSH, or a dry-run mock), renders the compact
report, and delivers it via ``openclaw message send``.

Safety defaults (CLAUDE.md §14 — dry-run first, fail closed):

* Without ``--send`` it only prints the report — no message is delivered.
* Without a ``--target`` (or ``OS_TELEGRAM_TARGET``) it refuses to send.
* The recipient id and channel are never hard-coded here; they come from the
  timer/env on the gateway host so nothing sensitive is committed.

Flags:

* ``--live``            collect real status over SSH (default: dry-run mock).
* ``--send``            actually deliver (default: print only).
* ``--only-incidents``  deliver only when something is WARNING or worse.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from os_system_agent.catalog import load_catalog
from os_system_agent.collector import build_chat_report, collect_statuses
from os_system_agent.notify import (
    Sender,
    default_sender,
    send_chunked,
    send_via_openclaw,  # re-exported for tests
    split_message,  # re-exported for tests
    telegram_direct_sender,
)
from os_system_agent.reports.daily import overall_severity
from os_system_agent.severity import Severity

__all__ = ["main", "send_via_openclaw", "should_send", "split_message"]

DEFAULT_CATALOG = Path("config/alert-rules.example.yml")
DEFAULT_SERVER_ALIAS = "server232"
DEFAULT_CHANNEL = "telegram"

# Severities that count as "something to alert about" for --only-incidents.
INCIDENT_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.WARNING, Severity.CRITICAL, Severity.SECURITY}
)


def should_send(worst: Severity, *, only_incidents: bool) -> bool:
    """Whether to deliver, given the worst severity and the --only-incidents gate."""
    if not only_incidents:
        return True
    return worst in INCIDENT_SEVERITIES


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
        "--direct",
        action="store_true",
        help="deliver via the Telegram Bot API directly (needs TELEGRAM_BOT_TOKEN), not OpenClaw",
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
    report = build_chat_report(jobs, statuses, now)
    worst = overall_severity(statuses)

    if not should_send(worst, only_incidents=args.only_incidents):
        print(f"[send_daily_report] overall={worst.value}; nothing to alert (--only-incidents).")
        return 0

    if not args.send:
        print(report)
        print(
            f"\n[dry-run] would send {len(split_message(report))} message(s) to "
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
        active_sender = sender
    elif args.direct:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print(
                "[send_daily_report] --direct needs TELEGRAM_BOT_TOKEN; refusing to send.",
                file=sys.stderr,
            )
            return 2  # fail closed
        active_sender = telegram_direct_sender(token)
    else:
        active_sender = default_sender(
            args.openclaw_bin or os.environ.get("OPENCLAW_BIN", "openclaw")
        )
    total = send_chunked(active_sender, args.channel, target, report)

    print(f"[send_daily_report] sent {total} message(s) to {args.channel}, overall={worst.value}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
