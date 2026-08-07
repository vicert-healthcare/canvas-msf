"""Tests for the aggregation engine.

The engine is handed a strategy and iterates the shared note selection, then
groups and counts in memory. The selection is replaced with a fixed set of stand
in notes so the grouping, the counting, the ordering, and the two skip branches
are all exercised without a database.
"""

import datetime
from types import SimpleNamespace

import arrow

from provider_note_dashboard.aggregation import engine
from provider_note_dashboard.aggregation.strategies import DayBucketing

ZONE = "UTC"
ANCHOR = arrow.get("2026-07-22T12:00:00+00:00")


def _provider(provider_id, name):
    return SimpleNamespace(id=provider_id, full_name=name)


def _note(provider, day):
    return SimpleNamespace(
        provider=provider,
        datetime_of_service=datetime.datetime(2026, 7, day, 9, 0, 0),
    )


def _patch_selection(mocker, notes):
    """Replace select_notes so its select_related returns the given notes."""
    queryset = SimpleNamespace(select_related=lambda *a, **k: notes)
    mocker.patch.object(engine, "select_notes", return_value=queryset)


def test_counts_group_per_provider_with_totals_and_per_bucket(mocker) -> None:
    doctor_a = _provider("a", "Dr A")
    doctor_b = _provider("b", "Dr B")
    notes = [
        _note(doctor_a, 20),
        _note(doctor_a, 22),
        _note(doctor_b, 21),
    ]
    _patch_selection(mocker, notes)

    result = engine.aggregate_counts(DayBucketing(), ANCHOR, 3, ZONE)

    rows = {row["provider_id"]: row for row in result["providers"]}
    assert rows["a"]["total"] == 2
    assert rows["a"]["counts"] == {"2026-07-20": 1, "2026-07-22": 1}
    assert rows["b"]["total"] == 1
    assert rows["b"]["counts"] == {"2026-07-21": 1}


def test_providers_are_sorted_busiest_first(mocker) -> None:
    doctor_a = _provider("a", "Dr A")
    doctor_b = _provider("b", "Dr B")
    notes = [
        _note(doctor_b, 20),
        _note(doctor_a, 20),
        _note(doctor_a, 21),
    ]
    _patch_selection(mocker, notes)

    result = engine.aggregate_counts(DayBucketing(), ANCHOR, 3, ZONE)

    assert [row["provider_id"] for row in result["providers"]] == ["a", "b"]


def test_a_note_with_no_provider_is_skipped(mocker) -> None:
    doctor_a = _provider("a", "Dr A")
    notes = [_note(doctor_a, 20), _note(None, 21)]
    _patch_selection(mocker, notes)

    result = engine.aggregate_counts(DayBucketing(), ANCHOR, 3, ZONE)

    assert len(result["providers"]) == 1
    assert result["providers"][0]["total"] == 1


def test_a_note_outside_the_buckets_is_skipped(mocker) -> None:
    doctor_a = _provider("a", "Dr A")
    # The tenth is inside the query window in reality only if the selection
    # returns it, but its key is not among the three buckets, so the engine's
    # own guard drops it from the counts.
    notes = [_note(doctor_a, 20), _note(doctor_a, 10)]
    _patch_selection(mocker, notes)

    result = engine.aggregate_counts(DayBucketing(), ANCHOR, 3, ZONE)

    assert result["providers"][0]["total"] == 1


def test_result_carries_the_view_window_and_buckets(mocker) -> None:
    _patch_selection(mocker, [])

    result = engine.aggregate_counts(DayBucketing(), ANCHOR, 3, ZONE)

    assert result["view"] == "day"
    assert [b["key"] for b in result["buckets"]] == [
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
    ]
    assert result["window"]["start"].startswith("2026-07-20")
    assert result["window"]["end"].startswith("2026-07-23")
    assert result["providers"] == []
