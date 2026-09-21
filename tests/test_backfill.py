"""Rules that decide what a backfill may overwrite (src/os_system_agent/backfill.py)."""

from __future__ import annotations

from os_system_agent.backfill import (
    ADDITIVE,
    RISKY,
    DayStats,
    Verdict,
    blocked_days,
    classify_day,
    classify_range,
    days_in_range,
    plan_ranges,
    summarize,
)

SITES = frozenset({"001", "002"})


def _stats(day: str, rows: int, sites: frozenset[str] = SITES, measure: float = 100.0) -> DayStats:
    return DayStats(day=day, rows=rows, sites=sites, measure=measure)


# --- classify_day ---------------------------------------------------------


def test_both_sides_empty_is_not_a_problem() -> None:
    """A holiday: the store closed, the zero is correct on both sides."""
    assert classify_day(None, None) is Verdict.BOTH_EMPTY
    assert classify_day(_stats("20260807", 0), _stats("20260807", 0)) is Verdict.BOTH_EMPTY


def test_source_has_rows_and_destination_none_is_loadable() -> None:
    assert classify_day(_stats("20260827", 23541), None) is Verdict.MISSING
    assert classify_day(_stats("20260827", 23541), _stats("20260827", 0)) is Verdict.MISSING


def test_destination_holds_what_the_source_lost() -> None:
    """The 2026-08 case: reloading would delete the only surviving copy."""
    verdict = classify_day(None, _stats("20260801", 14066, measure=588_931_157.0))
    assert verdict is Verdict.SOURCE_EMPTY
    assert verdict in RISKY


def test_a_site_missing_from_the_source_blocks_the_day() -> None:
    """Sede 002 posts late: the row count still looks plausible, the site does not."""
    source = _stats("20260811", 10_000, sites=frozenset({"001"}), measure=95.0)
    dest = _stats("20260811", 12_288, sites=SITES, measure=100.0)
    assert classify_day(source, dest) is Verdict.RISKY_SEDE


def test_a_materially_smaller_measure_blocks_the_day() -> None:
    source = _stats("20260801", 8_381, measure=60.0)
    dest = _stats("20260801", 14_066, measure=100.0)
    assert classify_day(source, dest) is Verdict.RISKY_MEASURE


def test_measure_tolerance_absorbs_rounding_but_not_a_real_gap() -> None:
    dest = _stats("20260901", 100, measure=1000.0)
    assert classify_day(_stats("20260901", 100, measure=995.0), dest) is Verdict.OK
    assert classify_day(_stats("20260901", 100, measure=989.0), dest) is Verdict.RISKY_MEASURE


def test_measure_comparison_can_be_turned_off() -> None:
    """Margen and rotacion measure a different total than the raw source does."""
    source = _stats("20260901", 100, measure=1.0)
    dest = _stats("20260901", 100, measure=1000.0)
    assert classify_day(source, dest, compare_measure=False) is Verdict.OK
    assert classify_day(source, dest, compare_measure=True) is Verdict.RISKY_MEASURE


def test_a_grid_day_loaded_with_no_sales_is_hollow_not_ok() -> None:
    """Rotacion returns ~10.600 rows a day even with zero sales — the blind spot."""
    source = _stats("20260901", 27_000, measure=440_000_000.0)
    dest = _stats("20260901", 10_600, measure=0.0)
    verdict = classify_day(source, dest, compare_measure=False)
    assert verdict is Verdict.HOLLOW
    assert verdict in ADDITIVE


def test_a_hollow_day_is_worth_a_range_of_its_own() -> None:
    assert plan_ranges({"20260901": Verdict.HOLLOW}) == [("20260901", "20260901")]


def test_a_hollow_day_joins_the_missing_days_around_it() -> None:
    verdicts = {
        "20260901": Verdict.MISSING,
        "20260902": Verdict.HOLLOW,
        "20260903": Verdict.MISSING,
    }
    assert plan_ranges(verdicts) == [("20260901", "20260903")]


def test_a_missing_site_still_wins_over_hollowness() -> None:
    """Losing a site is the expensive mistake; it is checked first on purpose."""
    source = _stats("20260901", 100, sites=frozenset({"001"}), measure=50.0)
    dest = _stats("20260901", 100, sites=SITES, measure=0.0)
    assert classify_day(source, dest) is Verdict.RISKY_SEDE


def test_a_zero_measure_destination_never_trips_the_measure_rule() -> None:
    """Dividing lines are drawn on the destination; a zero there proves nothing."""
    source = _stats("20260901", 100, measure=0.0)
    dest = _stats("20260901", 100, measure=0.0)
    assert classify_day(source, dest) is Verdict.OK


def test_extra_site_in_the_source_is_fine() -> None:
    """A site the destination lacks is exactly what a reload should bring in."""
    source = _stats("20260901", 100, sites=SITES)
    dest = _stats("20260901", 90, sites=frozenset({"001"}))
    assert classify_day(source, dest) is Verdict.OK


# --- days_in_range --------------------------------------------------------


def test_days_in_range_is_inclusive_on_both_ends() -> None:
    assert days_in_range("20260901", "20260901") == ["20260901"]
    assert days_in_range("20260830", "20260902") == [
        "20260830",
        "20260831",
        "20260901",
        "20260902",
    ]


def test_days_in_range_crosses_a_leap_day() -> None:
    assert days_in_range("20280228", "20280301") == ["20280228", "20280229", "20280301"]


# --- plan_ranges ----------------------------------------------------------


def test_contiguous_missing_days_become_one_range() -> None:
    verdicts = dict.fromkeys(days_in_range("20260827", "20260909"), Verdict.MISSING)
    assert plan_ranges(verdicts) == [("20260827", "20260909")]


def test_an_already_loaded_day_splits_the_plan() -> None:
    verdicts = {
        "20260901": Verdict.MISSING,
        "20260902": Verdict.OK,
        "20260903": Verdict.MISSING,
    }
    assert plan_ranges(verdicts) == [("20260901", "20260901"), ("20260903", "20260903")]


def test_a_risky_day_is_never_swallowed_by_a_range() -> None:
    """The whole point: a range must not re-extract a day that would lose data."""
    verdicts = {
        "20260901": Verdict.MISSING,
        "20260902": Verdict.SOURCE_EMPTY,
        "20260903": Verdict.MISSING,
    }
    plan = plan_ranges(verdicts)
    assert plan == [("20260901", "20260901"), ("20260903", "20260903")]
    assert all("20260902" not in (start, end) for start, end in plan)


def test_a_holiday_between_missing_days_stays_inside_the_range() -> None:
    verdicts = {
        "20260906": Verdict.MISSING,
        "20260907": Verdict.BOTH_EMPTY,
        "20260908": Verdict.MISSING,
    }
    assert plan_ranges(verdicts) == [("20260906", "20260908")]


def test_a_run_of_only_empty_days_is_not_worth_loading() -> None:
    verdicts = dict.fromkeys(days_in_range("20260906", "20260908"), Verdict.BOTH_EMPTY)
    assert plan_ranges(verdicts) == []


def test_a_calendar_gap_splits_the_plan() -> None:
    """Days absent from the mapping are not contiguous, even if they sort next to each other."""
    verdicts = {"20260901": Verdict.MISSING, "20260903": Verdict.MISSING}
    assert plan_ranges(verdicts) == [("20260901", "20260901"), ("20260903", "20260903")]


def test_nothing_to_do_is_an_empty_plan() -> None:
    assert plan_ranges({}) == []
    assert plan_ranges(dict.fromkeys(days_in_range("20260901", "20260905"), Verdict.OK)) == []


# --- reporting helpers ----------------------------------------------------


def test_blocked_days_lists_reasons_in_date_order() -> None:
    verdicts = {
        "20260903": Verdict.RISKY_SEDE,
        "20260901": Verdict.SOURCE_EMPTY,
        "20260902": Verdict.OK,
    }
    assert blocked_days(verdicts) == [
        ("20260901", Verdict.SOURCE_EMPTY),
        ("20260903", Verdict.RISKY_SEDE),
    ]


def test_summarize_counts_every_verdict() -> None:
    counts = summarize([Verdict.OK, Verdict.OK, Verdict.MISSING])
    assert counts == {Verdict.OK: 2, Verdict.MISSING: 1}


def test_additive_and_risky_do_not_overlap() -> None:
    assert not ADDITIVE & RISKY


# --- the real incident, as a regression test ------------------------------


def test_the_2026_09_21_dinastia_case() -> None:
    """ERP lost 20260801..20260820; GCP lacked 20260827..20260909.

    The plan must load the 14 missing days as a single range and refuse to
    touch the 20 days that only exist in the destination.
    """
    source = {
        day: _stats(day, 25_000, measure=400_000_000.0)
        for day in days_in_range("20260821", "20260909")
    }
    dest = {
        day: _stats(day, 8_000, measure=400_000_000.0)
        for day in days_in_range("20260801", "20260826")
        if day != "20260807"
    }
    days = days_in_range("20260801", "20260909")
    verdicts = classify_range(days, source, dest)

    assert plan_ranges(verdicts) == [("20260827", "20260909")]
    assert len(blocked_days(verdicts)) == 19
    assert verdicts["20260807"] is Verdict.BOTH_EMPTY
    assert verdicts["20260821"] is Verdict.OK
    assert summarize(verdicts.values())[Verdict.MISSING] == 14
