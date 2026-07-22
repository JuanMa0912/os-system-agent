"""Deliver notifications through OpenClaw, or directly via the Telegram Bot API.

Shared by the daily push and the proactive alerts so there is one send path.
Two backends implement the same :data:`Sender` shape:

* :func:`default_sender` — invokes the fixed ``openclaw message send`` (no shell).
* :func:`telegram_direct_sender` — POSTs to the Telegram Bot API directly, for
  lightweight per-empresa boxes that only send reports and don't run OpenClaw.

Long messages are split to stay under Telegram's 4096-char cap.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from os_system_agent.redaction import redact

# Telegram caps a message at 4096 chars; keep headroom for a chunk prefix.
CHUNK_LIMIT = 3900

# Telegram Bot API base (fixed https host; the bot token is a path segment).
TELEGRAM_API_BASE = "https://api.telegram.org"

# A message-send callable: (channel, target, message) -> None. Injected in tests
# so the send path is exercised without invoking the real openclaw CLI / network.
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


def send_via_telegram_api(
    *,
    target: str,
    message: str,
    token: str,
    timeout: float = 30.0,
    api_base: str = TELEGRAM_API_BASE,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Deliver one message directly via the Telegram Bot API (no OpenClaw).

    POSTs JSON to ``/bot<token>/sendMessage``. The bot token is a secret carried
    in the URL, so the URL is NEVER put in an exception or log; error text is also
    passed through :func:`redact` as a safety net. ``opener`` is injected in tests.
    """
    url = f"{api_base}/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": target, "text": message}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        response = opener(request, timeout=timeout)
        body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx from Telegram
        detail = redact(exc.read().decode("utf-8", "replace")) if hasattr(exc, "read") else ""
        raise RuntimeError(f"telegram sendMessage failed (HTTP {exc.code}): {detail}") from None
    except urllib.error.URLError as exc:  # network/DNS/TLS — never includes the URL
        raise RuntimeError(
            f"telegram sendMessage failed (network): {redact(str(exc.reason))}"
        ) from None
    data = json.loads(body) if body.strip() else {}
    if not data.get("ok", False):
        raise RuntimeError(f"telegram sendMessage rejected: {redact(str(data))}")


def telegram_direct_sender(token: str, *, api_base: str = TELEGRAM_API_BASE) -> Sender:
    """Return a :data:`Sender` that delivers via the Telegram Bot API directly.

    For lightweight per-empresa boxes that only send reports and don't run
    OpenClaw. ``channel`` is ignored (always Telegram); ``target`` is the chat id.
    """

    def _send(channel: str, target: str, message: str) -> None:
        send_via_telegram_api(target=target, message=message, token=token, api_base=api_base)

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
