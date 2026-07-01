"""Secret redaction helpers.

Pure, dependency-free functions applied before any text is logged, reported,
or sent to a channel. Principle #1: no secrets in logs, reports, prompts, or
notifications. This is a safety net, not a substitute for keeping secrets out
of the data in the first place.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

# Simple mask-the-whole-match / keep-groups patterns, applied in order.
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
_PROVIDER_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{20,}"
    r"|gh[posur]_[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
# Password inside a connection URI: scheme://user:PASSWORD@host
_URI_PASSWORD = re.compile(r"(://[^:@/\s]+:)[^@/\s]+(@)")
# key = value / key: value where the key name implies a secret.
_KV_SECRET = re.compile(
    r"(?i)\b([\w-]*?(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key))"
    r"(\s*[=:]\s*)(\"?)([^\s\"',]+)"
)


def _mask_kv(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"


def redact(text: str) -> str:
    """Return ``text`` with known secret shapes masked.

    Conservative by design: masks well-known token shapes (Telegram bot
    tokens, common provider keys), passwords embedded in connection URIs, and
    secret-looking ``key=value`` pairs.
    """
    if not text:
        return text
    out = _TELEGRAM_TOKEN.sub(REDACTED, text)
    out = _PROVIDER_KEY.sub(REDACTED, out)
    out = _URI_PASSWORD.sub(rf"\1{REDACTED}\2", out)
    out = _KV_SECRET.sub(_mask_kv, out)
    return out
