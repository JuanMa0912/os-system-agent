"""Deliver notifications through the OpenClaw CLI.

Shared by the daily push and the proactive alerts so there is one send path.
Invokes the fixed ``openclaw message send`` command (no shell) and splits long
messages to stay under Telegram's 4096-char cap.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# Telegram caps a message at 4096 chars; keep headroom for a chunk prefix.
CHUNK_LIMIT = 3900

# A message-send callable: (channel, target, message) -> None. Injected in tests
# so the send path is exercised without invoking the real openclaw CLI.
Sender = Callable[[str, str, str], None]


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


def default_sender(binary: str) -> Sender:
    """Return a :data:`Sender` that delivers through the openclaw CLI ``binary``."""

    def _send(channel: str, target: str, message: str) -> None:
        send_via_openclaw(channel=channel, target=target, message=message, binary=binary)

    return _send


def send_chunked(
    sender: Sender,
    channel: str,
    target: str,
    message: str,
) -> int:
    """Send ``message`` in Telegram-sized chunks via ``sender``; return chunk count."""
    chunks = split_message(message)
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        prefix = f"({index}/{total})\n" if total > 1 else ""
        sender(channel, target, prefix + chunk)
    return total
