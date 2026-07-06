"""Read-only MCP server exposing a single tool: ``estado_etl`` (spec 002).

Runs on the OpenClaw gateway host over stdio. The one tool it exposes is
**parameterless** and runs only the fixed read-only ETL collector — the same one
the daily push uses — and returns the rendered daily report. There is no way to
pass a command, path, or argument through it, so neither the model nor an
injected message can make it do anything but read status (CLAUDE.md §17
READ_ONLY).

Config comes from the environment (set in the ``openclaw mcp add`` definition, so
nothing sensitive is committed):

* ``OS_ETL_CATALOG``  — path to the real catalog (default ``config/alert-rules.yml``).
* ``OS_SERVER_ALIAS`` — SSH alias for the ETL server (default ``server232``).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from os_system_agent.catalog import CatalogError, load_catalog
from os_system_agent.collector import Runner, build_daily_report, collect_statuses
from os_system_agent.reports.daily import overall_severity
from os_system_agent.ssh_client import run_read_only

DEFAULT_CATALOG = Path(os.environ.get("OS_ETL_CATALOG", "config/alert-rules.yml"))
DEFAULT_ALIAS = os.environ.get("OS_SERVER_ALIAS", "server232")

# Short in-process cache so repeated asks don't hammer the ETL server over SSH.
_CACHE_TTL_SECONDS = 20.0


def current_report(
    *,
    catalog_path: Path = DEFAULT_CATALOG,
    alias: str = DEFAULT_ALIAS,
    now: datetime | None = None,
    runner: Runner = run_read_only,
) -> str:
    """Collect live read-only status and render the daily report (no side effects).

    Injectable (``catalog_path``/``alias``/``now``/``runner``) so the core is
    testable without a real server.
    """
    jobs = load_catalog(catalog_path)
    when = now or datetime.now(UTC)
    statuses = collect_statuses(jobs, live=True, alias=alias, now=when, runner=runner)
    # One structured, secret-free line for the gateway logs.
    print(
        f'{{"tool":"estado_etl","jobs":{len(statuses)},'
        f'"overall":"{overall_severity(statuses).value}"}}',
        file=sys.stderr,
        flush=True,
    )
    return build_daily_report(jobs, statuses, when)


class _ReportCache:
    """Tiny TTL cache for the rendered report (server-lifetime state)."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._at: float | None = None
        self._text: str | None = None

    def get(self, produce: Callable[[], str], *, monotonic: float) -> str:
        if self._at is not None and self._text is not None and monotonic - self._at < self._ttl:
            return self._text
        text = produce()
        self._at, self._text = monotonic, text
        return text


_cache = _ReportCache(_CACHE_TTL_SECONDS)

mcp = FastMCP("os-system-agent")


def _estado_etl_impl() -> str:
    """Tool body (plain function so it is unit-testable without MCP transport)."""
    try:
        return _cache.get(current_report, monotonic=time.monotonic())
    except CatalogError:
        return "ETL status unavailable: catalog error. Check the agent configuration."
    except Exception:  # never leak a stack trace/path to the channel
        return "ETL status unavailable: could not reach the ETL server. Try again shortly."


@mcp.tool()
def estado_etl() -> str:
    """Return the current read-only ETL status report for all monitored jobs.

    Read-only: collects systemd job status over SSH and renders the daily
    report. Takes no arguments, runs nothing else, and changes no state.
    """
    return _estado_etl_impl()


def main() -> None:
    """Run the MCP server over stdio (how OpenClaw launches it)."""
    mcp.run()


if __name__ == "__main__":
    main()
