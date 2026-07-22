"""Tests for the Telegram push entrypoint (scripts/send_daily_report.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import send_daily_report as sdr
from os_system_agent.notify import send_via_telegram_api
from os_system_agent.severity import Severity

MINIMAL_CATALOG = """\
empresa: TestCo
jobs:
  - id: test-job
    name: Test Job
    server: testserver
    freshness:
      max_delay_minutes_warning: 1500
      max_delay_minutes_critical: 1560
"""


class _Recorder:
    """A fake Sender that records every (channel, target, message) call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, channel: str, target: str, message: str) -> None:
        self.calls.append((channel, target, message))


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.yml"
    path.write_text(MINIMAL_CATALOG, encoding="utf-8")
    return path


# --- split_message ---------------------------------------------------------

def test_split_message_keeps_short_text_intact() -> None:
    assert sdr.split_message("hola\nmundo") == ["hola\nmundo"]


def test_split_message_chunks_and_reconstructs() -> None:
    text = "\n".join(f"line {i}" for i in range(2000))
    chunks = sdr.split_message(text, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_message_hard_slices_an_overlong_line() -> None:
    text = "x" * 500
    chunks = sdr.split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


# --- should_send -----------------------------------------------------------

def test_should_send_always_when_not_only_incidents() -> None:
    assert sdr.should_send(Severity.INFO, only_incidents=False) is True


def test_should_send_gates_on_incidents() -> None:
    assert sdr.should_send(Severity.INFO, only_incidents=True) is False
    assert sdr.should_send(Severity.WARNING, only_incidents=True) is True
    assert sdr.should_send(Severity.CRITICAL, only_incidents=True) is True


# --- send_via_openclaw -----------------------------------------------------

def test_send_via_openclaw_builds_argv_without_shell() -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    sdr.send_via_openclaw(
        channel="telegram", target="123", message="hi", binary="openclaw", run=fake_run
    )
    assert seen["cmd"] == [
        "openclaw", "message", "send",
        "--channel", "telegram", "--target", "123", "--message", "hi",
    ]


def test_send_via_openclaw_raises_on_nonzero_exit() -> None:
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(RuntimeError, match="exit 1"):
        sdr.send_via_openclaw(channel="telegram", target="123", message="hi", run=fake_run)


# --- main ------------------------------------------------------------------

def test_main_dry_run_does_not_send(catalog: Path) -> None:
    recorder = _Recorder()
    rc = sdr.main(["--catalog", str(catalog), "--target", "123"], sender=recorder)
    assert rc == 0
    assert recorder.calls == []


def test_main_send_delivers_report(catalog: Path) -> None:
    recorder = _Recorder()
    rc = sdr.main(
        ["--catalog", str(catalog), "--send", "--target", "123"], sender=recorder
    )
    assert rc == 0
    assert len(recorder.calls) == 1
    channel, target, message = recorder.calls[0]
    assert channel == "telegram"
    assert target == "123"
    assert "OS_SYSTEM_AGENT · ETL" in message


def test_main_send_without_target_fails_closed(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OS_TELEGRAM_TARGET", raising=False)
    recorder = _Recorder()
    rc = sdr.main(["--catalog", str(catalog), "--send"], sender=recorder)
    assert rc == 2
    assert recorder.calls == []


def test_main_only_incidents_stays_quiet_when_healthy(catalog: Path) -> None:
    recorder = _Recorder()
    rc = sdr.main(
        ["--catalog", str(catalog), "--send", "--target", "123", "--only-incidents"],
        sender=recorder,
    )
    assert rc == 0
    assert recorder.calls == []  # dry-run mock is fresh -> INFO -> nothing to alert


# --- direct Telegram delivery (no OpenClaw) --------------------------------

def test_send_via_telegram_api_posts_expected_payload() -> None:
    seen: dict[str, object] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"ok": true, "result": {}}'

    def fake_opener(request, timeout):  # type: ignore[no-untyped-def]
        seen["url"] = request.full_url
        seen["data"] = request.data
        seen["method"] = request.get_method()
        return _Resp()

    send_via_telegram_api(target="123", message="hola", token="777:tok", opener=fake_opener)
    assert seen["url"] == "https://api.telegram.org/bot777:tok/sendMessage"
    assert seen["method"] == "POST"
    assert b'"chat_id": "123"' in seen["data"]  # type: ignore[operator]
    assert b"hola" in seen["data"]  # type: ignore[operator]


def test_main_direct_without_token_fails_closed(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # --direct selected but no token -> fail closed, nothing sent.
    rc = sdr.main(["--catalog", str(catalog), "--send", "--direct", "--target", "123"])
    assert rc == 2
