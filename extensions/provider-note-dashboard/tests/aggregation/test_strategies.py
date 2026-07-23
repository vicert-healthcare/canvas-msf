"""Tests for the three concrete bucketing strategies.

The strategies are pure date math over an arrow anchor, so they test directly
against a fixed moment with no database and no Canvas runtime. The anchor is a
Wednesday so the week floor to Monday is visible in the assertions.
"""

import re

import arrow

from provider_note_dashboard.aggregation.strategies import (
    DayBucketing,
    MonthBucketing,
    WeekBucketing,
)

# A fixed anchor, Wednesday the twenty second of July twenty twenty six, noon
# UTC, so the day, week, and month floors are all unambiguous.
ANCHOR = arrow.get("2026-07-22T12:00:00+00:00")


def test_day_default_periods() -> None:
    assert DayBucketing().default_periods == 14


def test_day_window_is_half_open_and_ends_after_the_anchor_day() -> None:
    start, end = DayBucketing().window(ANCHOR, 3)
    assert start == arrow.get("2026-07-20T00:00:00+00:00")
    assert end == arrow.get("2026-07-23T00:00:00+00:00")


def test_day_buckets_are_the_expected_keys_and_labels() -> None:
    buckets = DayBucketing().buckets(ANCHOR, 3)
    assert [b["key"] for b in buckets] == ["2026-07-20", "2026-07-21", "2026-07-22"]
    assert [b["label"] for b in buckets] == ["Jul 20", "Jul 21", "Jul 22"]


def test_day_key_for_matches_the_last_bucket() -> None:
    buckets = DayBucketing().buckets(ANCHOR, 3)
    assert DayBucketing().key_for(ANCHOR) == buckets[-1]["key"]


def test_week_default_periods() -> None:
    assert WeekBucketing().default_periods == 8


def test_week_window_floors_to_monday_and_is_half_open() -> None:
    start, end = WeekBucketing().window(ANCHOR, 2)
    # The anchor week floors to Monday the twentieth, one prior week back is the
    # thirteenth, and the exclusive end is the Monday after the anchor week.
    assert start == arrow.get("2026-07-13T00:00:00+00:00")
    assert end == arrow.get("2026-07-27T00:00:00+00:00")


def test_week_buckets_have_iso_week_keys_and_a_week_of_label() -> None:
    buckets = WeekBucketing().buckets(ANCHOR, 2)
    assert len(buckets) == 2
    for bucket in buckets:
        assert re.fullmatch(r"\d{4}-W\d{2}", bucket["key"])
        assert bucket["label"].startswith("Week of ")


def test_week_key_for_the_anchor_matches_the_last_bucket() -> None:
    buckets = WeekBucketing().buckets(ANCHOR, 2)
    assert WeekBucketing().key_for(ANCHOR) == buckets[-1]["key"]


def test_month_default_periods() -> None:
    assert MonthBucketing().default_periods == 6


def test_month_window_floors_to_the_first_and_is_half_open() -> None:
    start, end = MonthBucketing().window(ANCHOR, 3)
    assert start == arrow.get("2026-05-01T00:00:00+00:00")
    assert end == arrow.get("2026-08-01T00:00:00+00:00")


def test_month_buckets_are_the_expected_keys_and_labels() -> None:
    buckets = MonthBucketing().buckets(ANCHOR, 3)
    assert [b["key"] for b in buckets] == ["2026-05", "2026-06", "2026-07"]
    assert [b["label"] for b in buckets] == ["May 2026", "Jun 2026", "Jul 2026"]


def test_month_key_for_matches_the_last_bucket() -> None:
    buckets = MonthBucketing().buckets(ANCHOR, 3)
    assert MonthBucketing().key_for(ANCHOR) == buckets[-1]["key"]


def test_a_bucket_count_always_equals_the_requested_periods() -> None:
    for strategy in (DayBucketing(), WeekBucketing(), MonthBucketing()):
        assert len(strategy.buckets(ANCHOR, 5)) == 5
