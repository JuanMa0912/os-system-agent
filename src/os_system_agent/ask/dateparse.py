"""Timezone-explicit business dates (spec 006 RNF-01, plan §5).

The repo runs in UTC and the business runs in local time. A "yesterday"
computed from the host clock returns the wrong day for several hours of every
day — silently, with plausible figures. That is the worst failure mode
available: nobody notices, and the number is wrong.

So every date this package uses comes from here, the zone always comes from the
intent catalog (never a buried constant), and ``date.today()`` /
``datetime.now()`` without a zone are banned from the whole ``ask`` package.

Both public functions accept an injected ``now`` so callers and tests can pin
the instant; an injected *naive* datetime is rejected, because a naive
datetime is exactly how the UTC bug gets back in.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from os_system_agent.redaction import redact

# A timezone is either an IANA name from the catalog ("America/Bogota") or an
# already-resolved tzinfo. Accepting both keeps the zone injectable, which is
# what lets the day-border tests run on hosts with no tz database installed.
TzLike = str | tzinfo

# Canonical expressions the agent understands. Anything else fails closed: an
# unrecognised phrase must never quietly become "hoy".
SUPPORTED_EXPRESSIONS: tuple[str, ...] = (
    "hoy",
    "ayer",
    "esta semana",
    "semana pasada",
    "este mes",
    "mes pasado",
    "YYYY-MM-DD",
    "YYYY-MM-DD..YYYY-MM-DD",
)

# Free text arriving from a chat may be long or carry a secret someone pasted.
# Error messages echo at most this many characters, and only after redaction.
_MAX_ECHO_CHARS = 60

_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_SPAN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})$")

# Aliases -> canonical expression. Written without accents on purpose: the
# input is normalised (accents stripped, lowercased) before the lookup, so
# "Mes Pasado", "mes pasado" and "el mes pasado" all land on the same key.
_ALIASES: dict[str, str] = {
    "hoy": "hoy",
    "dia de hoy": "hoy",
    "el dia de hoy": "hoy",
    "ayer": "ayer",
    "dia de ayer": "ayer",
    "el dia de ayer": "ayer",
    "esta semana": "esta semana",
    "semana actual": "esta semana",
    "la semana actual": "esta semana",
    "semana pasada": "semana pasada",
    "la semana pasada": "semana pasada",
    "semana anterior": "semana pasada",
    "este mes": "este mes",
    "mes actual": "este mes",
    "el mes actual": "este mes",
    "mes pasado": "mes pasado",
    "el mes pasado": "mes pasado",
    "mes anterior": "mes pasado",
}


class DateExpressionError(RuntimeError):
    """Raised when a timezone or a date expression cannot be resolved."""


@dataclass(frozen=True)
class DateRange:
    """An inclusive business-day range, resolved in a explicit timezone.

    ``partial`` is True when the range reaches today: today's data is still
    being produced, so the figures it covers are not final. Spec 006 RF-05
    forbids presenting a partial aggregate as a complete one, and this flag is
    what lets the answer say so.
    """

    start: date
    end: date
    expression: str
    partial: bool

    def iso(self) -> tuple[str, str]:
        """Return ``(start, end)`` as ISO ``YYYY-MM-DD`` strings for query params."""
        return (self.start.isoformat(), self.end.isoformat())


def _safe_echo(text: str) -> str:
    """Return chat text safe to put in an error message.

    Redact BEFORE truncating: truncating first can cut a token in half, and
    half a token no longer matches the redaction patterns — it would leak.
    """
    return redact(" ".join(text.split()))[:_MAX_ECHO_CHARS]


def load_zone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, failing closed on anything unusable.

    Never falls back to UTC or to the host zone: a wrong zone silently shifts
    every business day, which is the bug this module exists to prevent.
    """
    if not isinstance(name, str) or not name.strip():
        raise DateExpressionError("timezone must be a non-empty IANA name (e.g. 'America/Bogota')")
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError as exc:
        # A host with no tz database at all (common on bare Windows) fails the
        # same way as a typo, so the message names both causes and the fix.
        raise DateExpressionError(
            f"unknown timezone {name.strip()!r}: check the spelling, or install the "
            "'tzdata' package if this host has no system time zone database"
        ) from exc
    except ValueError as exc:
        raise DateExpressionError(f"invalid timezone name {name.strip()!r}") from exc


def _resolve_zone(tz: TzLike) -> tzinfo:
    """Accept an IANA name or an already-resolved tzinfo."""
    if isinstance(tz, tzinfo):
        return tz
    return load_zone(tz)


def _require_aware(moment: datetime) -> datetime:
    """Reject naive datetimes: 'no zone' is how the UTC bug comes back."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DateExpressionError(
            "the injected 'now' must be timezone-aware; a naive datetime silently "
            "assumes the host zone and shifts the business day"
        )
    return moment


def now_local(tz: TzLike, *, now: datetime | None = None) -> datetime:
    """Return the current instant expressed in the business timezone."""
    zone = _resolve_zone(tz)
    moment = datetime.now(zone) if now is None else _require_aware(now)
    return moment.astimezone(zone)


def today_local(tz: TzLike, *, now: datetime | None = None) -> date:
    """Return today's *business* date in ``tz`` — never the host's UTC date."""
    return now_local(tz, now=now).date()


def _parse_iso_day(text: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` day, failing closed on impossible dates."""
    if not _ISO_DAY.match(text):
        raise DateExpressionError(f"not an ISO date: {_safe_echo(text)!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DateExpressionError(f"not a real calendar date: {_safe_echo(text)!r}") from exc


def _normalize(expr: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    stripped = expr.strip().strip("¿?¡!.,;:")
    decomposed = unicodedata.normalize("NFKD", stripped)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(without_accents.lower().split())


def _relative_bounds(key: str, today: date) -> tuple[date, date]:
    """Return the inclusive ``(start, end)`` for a canonical relative expression.

    Weeks start on Monday (ISO). Ranges that would reach into the future are
    cut at today: there is no data for tomorrow, and asking for it only
    produces a confusing empty answer.
    """
    if key == "hoy":
        return (today, today)
    if key == "ayer":
        yesterday = today - timedelta(days=1)
        return (yesterday, yesterday)
    if key == "esta semana":
        return (today - timedelta(days=today.weekday()), today)
    if key == "semana pasada":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        return (last_monday, last_monday + timedelta(days=6))
    if key == "este mes":
        return (today.replace(day=1), today)
    if key == "mes pasado":
        first_of_this_month = today.replace(day=1)
        last_of_previous = first_of_this_month - timedelta(days=1)
        return (last_of_previous.replace(day=1), last_of_previous)
    raise DateExpressionError(f"unsupported expression key: {key!r}")


def resolve_range(expr: str, tz: TzLike, *, now: datetime | None = None) -> DateRange:
    """Resolve a business date expression into an inclusive :class:`DateRange`.

    Accepts the relative expressions in :data:`SUPPORTED_EXPRESSIONS` plus an
    explicit ISO day (``2026-08-24``) or ISO span (``2026-08-01..2026-08-15``).
    Anything else raises :class:`DateExpressionError`; an unrecognised phrase
    must never quietly become "today".
    """
    if not isinstance(expr, str) or not expr.strip():
        raise DateExpressionError(
            "empty date expression; expected one of: " + ", ".join(SUPPORTED_EXPRESSIONS)
        )

    today = today_local(tz, now=now)
    normalized = _normalize(expr)

    span = _ISO_SPAN.match(normalized)
    if span:
        start = _parse_iso_day(span.group(1))
        end = _parse_iso_day(span.group(2))
        if start > end:
            raise DateExpressionError(
                f"date range starts after it ends: {start.isoformat()} > {end.isoformat()}"
            )
        return DateRange(
            start=start,
            end=end,
            expression=f"{start.isoformat()}..{end.isoformat()}",
            partial=end >= today,
        )

    if _ISO_DAY.match(normalized):
        day = _parse_iso_day(normalized)
        return DateRange(start=day, end=day, expression=day.isoformat(), partial=day >= today)

    key = _ALIASES.get(normalized)
    if key is None:
        raise DateExpressionError(
            f"unsupported date expression {_safe_echo(expr)!r}; expected one of: "
            + ", ".join(SUPPORTED_EXPRESSIONS)
        )

    start, end = _relative_bounds(key, today)
    return DateRange(start=start, end=end, expression=key, partial=end >= today)
