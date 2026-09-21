"""Decide which days may be reloaded from the source — and which must not be.

The rules here are the safety layer of any backfill that writes with
``replace_by_date`` (delete the day's partition, then insert what the source
returned). That write mode is idempotent when the source is complete and
destructive when it is not, so *deciding* is the dangerous part, not loading.

Hard-won on 2026-09-21 against Dinastia: the ERP had lost 24 days that only
existed in the destination. A blind reload of the whole range would have
deleted them — the loader's zero-row guard was the only thing in the way.
These classifiers make that case explicit instead of incidental.

Pure functions only: no I/O, no drivers. The caller gathers the per-day stats
from wherever they live and gets back a verdict plus a plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

DATE_FMT = "%Y%m%d"


class Verdict(StrEnum):
    """What may be done with one day."""

    OK = "ok"
    """Destination already holds what the source has. Reloading is a no-op."""

    MISSING = "missing"
    """Source has rows, destination has none. Loading only adds — safe."""

    BOTH_EMPTY = "both_empty"
    """Neither side has rows (a holiday, a closed store). Nothing to do."""

    HOLLOW = "hollow"
    """Destination has rows but a zero measure while the source measures.

    A row count is not a completeness signal for a grid-shaped table: rotacion
    returns ~10.600 rows a day even with no sales at all, so a day loaded while
    the ERP was empty looks present and is in fact hollow. Reloading it can only
    gain — the source now has the sales the destination never got.
    """

    SOURCE_EMPTY = "source_empty"
    """Destination has rows the source no longer has. NEVER reload this day."""

    RISKY_SEDE = "risky_sede"
    """The destination holds a site the source is missing: reloading loses it."""

    RISKY_MEASURE = "risky_measure"
    """The source measures materially less than the destination: partial source."""


#: Verdicts a reload may touch. Everything else needs a human.
ADDITIVE = frozenset({Verdict.MISSING, Verdict.BOTH_EMPTY, Verdict.HOLLOW})

#: Verdicts that make a range worth running at all.
WORTH_LOADING = frozenset({Verdict.MISSING, Verdict.HOLLOW})

#: Verdicts that mean "reloading would destroy data".
RISKY = frozenset({Verdict.SOURCE_EMPTY, Verdict.RISKY_SEDE, Verdict.RISKY_MEASURE})


@dataclass(frozen=True)
class DayStats:
    """One day as seen on one side (source or destination).

    ``measure`` is the money/volume total used as a completeness signal. It is
    only comparable across sides when both compute it the same way, which is
    what ``compare_measure`` in :func:`classify_day` is for.
    """

    day: str
    rows: int = 0
    sites: frozenset[str] = field(default_factory=frozenset)
    measure: float = 0.0


def classify_day(
    source: DayStats | None,
    dest: DayStats | None,
    *,
    compare_measure: bool = True,
    measure_tolerance: float = 0.01,
) -> Verdict:
    """Classify one day by comparing the source against the destination.

    ``measure_tolerance`` is the fraction the source may fall below the
    destination before the day is called risky (default 1%), absorbing rounding
    and late-arriving cents without hiding a missing site's worth of sales.

    Fails closed: when the destination holds something the source cannot
    reproduce, the day is risky even if the row counts look reassuring.
    """
    src_rows = source.rows if source else 0
    dst_rows = dest.rows if dest else 0

    if src_rows == 0:
        return Verdict.BOTH_EMPTY if dst_rows == 0 else Verdict.SOURCE_EMPTY
    if dst_rows == 0:
        return Verdict.MISSING

    dst_sites = dest.sites if dest else frozenset()
    src_sites = source.sites if source else frozenset()
    if dst_sites - src_sites:
        return Verdict.RISKY_SEDE

    src_measure_now = source.measure if source else 0.0
    if dest and dest.measure == 0 and src_measure_now > 0:
        return Verdict.HOLLOW

    if compare_measure and dest and dest.measure > 0:
        src_measure = source.measure if source else 0.0
        if src_measure < dest.measure * (1.0 - measure_tolerance):
            return Verdict.RISKY_MEASURE

    return Verdict.OK


def days_in_range(start: str, end: str) -> list[str]:
    """Every ``YYYYMMDD`` from ``start`` to ``end``, both included."""
    first = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    last = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    out: list[str] = []
    while first <= last:
        out.append(first.strftime(DATE_FMT))
        first += timedelta(days=1)
    return out


def _is_next_day(previous: str, current: str) -> bool:
    prev = date(int(previous[:4]), int(previous[4:6]), int(previous[6:8]))
    curr = date(int(current[:4]), int(current[4:6]), int(current[6:8]))
    return curr - prev == timedelta(days=1)


def plan_ranges(verdicts: Mapping[str, Verdict]) -> list[tuple[str, str]]:
    """Group the loadable days into the fewest contiguous ranges.

    A range may only span additive days, and a run of them is kept only if it
    contains something worth loading — a stretch of empty holidays is pointless
    work.

    The grouping breaks on *any* non-additive day, which is the whole point: a
    risky day must never end up in the middle of a range, because loading the
    range would re-extract it from the source and replace what is there.
    """
    runs: list[list[str]] = []
    for day in sorted(verdicts):
        if verdicts[day] not in ADDITIVE:
            runs.append([])
            continue
        if runs and runs[-1] and _is_next_day(runs[-1][-1], day):
            runs[-1].append(day)
        else:
            runs.append([day])
    return [
        (run[0], run[-1])
        for run in runs
        if any(verdicts[d] in WORTH_LOADING for d in run)
    ]


def blocked_days(verdicts: Mapping[str, Verdict]) -> list[tuple[str, Verdict]]:
    """The days a reload must not touch, in date order, with their reason."""
    return [(d, verdicts[d]) for d in sorted(verdicts) if verdicts[d] in RISKY]


def summarize(verdicts: Iterable[Verdict]) -> dict[Verdict, int]:
    """Count how many days fell into each verdict."""
    counts: dict[Verdict, int] = {}
    for verdict in verdicts:
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def classify_range(
    days: Sequence[str],
    source: Mapping[str, DayStats],
    dest: Mapping[str, DayStats],
    *,
    compare_measure: bool = True,
    measure_tolerance: float = 0.01,
) -> dict[str, Verdict]:
    """Classify every day of ``days`` from two lookups keyed by ``YYYYMMDD``."""
    return {
        day: classify_day(
            source.get(day),
            dest.get(day),
            compare_measure=compare_measure,
            measure_tolerance=measure_tolerance,
        )
        for day in days
    }
