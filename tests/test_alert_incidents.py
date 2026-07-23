"""Tests for the proactive alert orchestrator (scripts/alert_incidents.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import alert_incidents as ai
from os_system_agent.monitors.freshness import JobStatus
from os_system_agent.severity import Severity


def _incident(job_id: str, severity: Severity = Severity.CRITICAL) -> JobStatus:
    return JobStatus(
        job_id=job_id,
        name=f"Job {job_id}",
        severity=severity,
        delay_minutes=None,
        latest_at=None,
        evidence=f"{job_id}.service",
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, channel: str, target: str, message: str) -> None:
        self.calls.append((channel, target, message))


def _fixed_incidents(*statuses: JobStatus):
    def _fn(catalog: Path, alias: str, now: object) -> tuple[list[JobStatus], str, str]:
        return list(statuses), "server232", "Mercamio"

    return _fn


def test_load_state_returns_empty_on_missing_or_bad(tmp_path: Path) -> None:
    assert ai._load_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert ai._load_state(bad) == {}


def test_sends_on_new_incident_then_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(ai, "_current_incidents", _fixed_incidents(_incident("ventas")))

    rec = _Recorder()
    rc = ai.main(["--send", "--target", "123", "--state-file", str(state)], sender=rec)
    assert rc == 0
    assert len(rec.calls) == 1
    assert "Job ventas" in rec.calls[0][2]
    assert "empresa Mercamio" in rec.calls[0][2]  # alert names the company
    assert json.loads(state.read_text(encoding="utf-8")) == {"ventas": "CRITICAL"}

    # Same incident on the next run -> no second alert (no fatigue).
    rec2 = _Recorder()
    rc2 = ai.main(["--send", "--target", "123", "--state-file", str(state)], sender=rec2)
    assert rc2 == 0
    assert rec2.calls == []


def test_recovery_sends_and_clears_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ventas": "CRITICAL"}), encoding="utf-8")
    monkeypatch.setattr(ai, "_current_incidents", _fixed_incidents())  # nothing active now

    rec = _Recorder()
    rc = ai.main(["--send", "--target", "123", "--state-file", str(state)], sender=rec)
    assert rc == 0
    assert len(rec.calls) == 1
    assert "Recuperados" in rec.calls[0][2]
    assert json.loads(state.read_text(encoding="utf-8")) == {}


def test_dry_run_does_not_send_or_touch_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(ai, "_current_incidents", _fixed_incidents(_incident("ventas")))
    rec = _Recorder()
    rc = ai.main(["--target", "123", "--state-file", str(state)], sender=rec)  # no --send
    assert rc == 0
    assert rec.calls == []
    assert not state.exists()


def test_send_without_target_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OS_TELEGRAM_TARGET", raising=False)
    state = tmp_path / "state.json"
    monkeypatch.setattr(ai, "_current_incidents", _fixed_incidents(_incident("ventas")))
    rec = _Recorder()
    rc = ai.main(["--send", "--state-file", str(state)], sender=rec)
    assert rc == 2
    assert rec.calls == []
    assert not state.exists()  # state not advanced -> alert retries next run


def test_direct_without_token_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    state = tmp_path / "state.json"
    monkeypatch.setattr(ai, "_current_incidents", _fixed_incidents(_incident("ventas")))
    # --direct selected but no token -> fail closed, nothing sent, state untouched.
    rc = ai.main(["--send", "--direct", "--target", "123", "--state-file", str(state)])
    assert rc == 2
    assert not state.exists()  # state not advanced -> alert retries next run
