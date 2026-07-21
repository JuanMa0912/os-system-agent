"""Tests for the read-only estado_etl MCP tool core (os_system_agent.mcp_server)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from os_system_agent import mcp_server
from os_system_agent.catalog import CatalogError
from os_system_agent.ssh_client import CommandResult

NOW = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)

CANNED_SHOW = (
    "Id=test.service\n"
    "Result=success\n"
    "ExecMainStatus=0\n"
    "ExecMainExitTimestamp=Mon 2026-07-06 07:56:59 -05\n"
    "ActiveState=inactive\n"
)

CATALOG = """\
empresa: TestCo
jobs:
  - id: test-job
    name: Test Job
    server: testserver
    systemd_unit: test.service
    freshness:
      max_delay_minutes_warning: 1500
      max_delay_minutes_critical: 1560
"""


def _runner(alias: str, command: str) -> CommandResult:
    return CommandResult(command=command, exit_code=0, stdout=CANNED_SHOW, stderr="")


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.yml"
    path.write_text(CATALOG, encoding="utf-8")
    return path


def test_current_report_renders_from_runner(catalog: Path) -> None:
    report = mcp_server.current_report(
        catalog_path=catalog, alias="server232", now=NOW, runner=_runner
    )
    assert "OS_SYSTEM_AGENT · ETL" in report
    assert "Test Job" in report
    assert "Estado: INFO" in report


def test_estado_etl_tool_takes_no_arguments() -> None:
    target = getattr(mcp_server.estado_etl, "fn", mcp_server.estado_etl)
    assert list(inspect.signature(target).parameters) == []


def test_report_cache_serves_within_ttl_then_refreshes() -> None:
    cache = mcp_server._ReportCache(20.0)
    calls: list[int] = []

    def produce() -> str:
        calls.append(1)
        return f"report-{len(calls)}"

    assert cache.get(produce, monotonic=100.0) == "report-1"
    assert cache.get(produce, monotonic=110.0) == "report-1"  # within TTL -> cached
    assert cache.get(produce, monotonic=125.0) == "report-2"  # past TTL -> refreshed
    assert len(calls) == 2


def test_impl_returns_safe_message_on_catalog_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_cache", mcp_server._ReportCache(20.0))

    def boom() -> str:
        raise CatalogError("bad catalog")

    monkeypatch.setattr(mcp_server, "current_report", boom)
    out = mcp_server._estado_etl_impl()
    assert "catalog error" in out
    assert "Traceback" not in out


def test_impl_returns_safe_message_on_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_cache", mcp_server._ReportCache(20.0))

    def boom() -> str:
        raise RuntimeError("ssh exploded at /home/juan/.ssh/key")

    monkeypatch.setattr(mcp_server, "current_report", boom)
    out = mcp_server._estado_etl_impl()
    assert "could not reach" in out
    assert "/home/juan" not in out  # no path/secret leak
