#!/usr/bin/env python3
"""alert_incidents.py — proactive ETL incident alerts (spec 003).

Live-checks ETL status and sends a Telegram alert ONLY when the incident set
changes: a new or escalated incident, a recovery, or the ETL server going
unreachable. Silent when nothing changed — no alert fatigue. Stateful via a
small JSON file. Read-only; never executes anything on the server.

Meant to run from a systemd timer every 1-2h (more often than the daily digest).

Safety defaults (CLAUDE.md §14): dry-run unless ``--send``; refuses to send
without a ``--target``/``OS_TELEGRAM_TARGET``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from os_system_agent.alerting import (
    SERVER_DOWN_ID,
    diff_incidents,
    incident_statuses,
    render_alert,
    server_down_status,
)
from os_system_agent.catalog import load_catalog
from os_system_agent.collector import collect_statuses
from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.notify import Sender, default_sender, send_chunked
from os_system_agent.ssh_client import run_read_only

DEFAULT_CATALOG = Path("config/alert-rules.yml")
DEFAULT_SERVER_ALIAS = "server232"
DEFAULT_CHANNEL = "telegram"
DEFAULT_STATE = Path(".alert-state.json")
PROBE_TIMEOUT = 10.0


def _server_reachable(alias: str) -> bool:
    """One quick read-only probe; False if SSH cannot reach the server."""
    try:
        result = run_read_only(alias, "hostname", timeout=PROBE_TIMEOUT)
    except Exception:  # timeout / transport error -> treat as unreachable
        return False
    return result.exit_code == 0


def _load_state(path: Path) -> dict[str, str]:
    """Load the last-alerted ``{job_id: severity}`` state; empty if absent/invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_state(path: Path, state: dict[str, str]) -> None:
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _current_incidents(catalog: Path, alias: str, now: datetime) -> tuple[list[JobStatus], str]:
    """Return (incident statuses, server) — a single server-down incident if unreachable."""
    jobs = load_catalog(catalog)
    server = jobs[0].server if jobs else alias
    if not _server_reachable(alias):
        return [server_down_status(server)], server
    statuses = collect_statuses(jobs, live=True, alias=alias, now=now)
    return incident_statuses(statuses), server


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send proactive ETL incident alerts.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--server-alias", default=DEFAULT_SERVER_ALIAS)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--target", default=None, help="falls back to OS_TELEGRAM_TARGET")
    parser.add_argument("--openclaw-bin", default=None, help="falls back to OPENCLAW_BIN")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--send", action="store_true", help="actually deliver (default: print)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, sender: Sender | None = None) -> int:
    args = _parse_args(argv)
    now = datetime.now(UTC)

    incidents, server = _current_incidents(args.catalog, args.server_alias, now)
    previous = _load_state(args.state_file)
    outcome = diff_incidents(previous, incidents)

    if not outcome.changed:
        print(f"[alert_incidents] no change ({len(incidents)} active incident(s)); staying silent.")
        _save_state(args.state_file, outcome.state)  # keep state current
        return 0

    # Names for the incident lines come from the statuses; add a friendly label
    # for the synthetic server-down id so a server recovery reads nicely.
    names = {s.job_id: s.name for s in incidents}
    names[SERVER_DOWN_ID] = "Servidor ETL"
    message = render_alert(
        server=server,
        report_date=now.date(),
        to_alert=outcome.to_alert,
        recovered=outcome.recovered,
        names=names,
    )

    if not args.send:
        print(message)
        print("\n[dry-run] would send this alert (pass --send to deliver).", file=sys.stderr)
        return 0

    target = args.target or os.environ.get("OS_TELEGRAM_TARGET")
    if not target:
        print(
            "[alert_incidents] no --target and OS_TELEGRAM_TARGET is unset; refusing to send.",
            file=sys.stderr,
        )
        return 2  # fail closed — do NOT update state, so the alert retries next run

    active_sender = sender or default_sender(
        args.openclaw_bin or os.environ.get("OPENCLAW_BIN", "openclaw")
    )
    send_chunked(active_sender, args.channel, target, message)
    _save_state(args.state_file, outcome.state)

    print(
        f"[alert_incidents] sent alert: {len(outcome.to_alert)} new/changed, "
        f"{len(outcome.recovered)} recovered."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
