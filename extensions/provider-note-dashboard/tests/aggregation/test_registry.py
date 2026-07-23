"""Tests for the strategy composition root."""

from provider_note_dashboard.aggregation.registry import (
    available_views,
    get_strategy,
)


def test_known_view_returns_its_strategy() -> None:
    assert get_strategy("day").view_key == "day"
    assert get_strategy("week").view_key == "week"
    assert get_strategy("month").view_key == "month"


def test_unknown_view_returns_none() -> None:
    assert get_strategy("year") is None


def test_available_views_are_the_three_in_registration_order() -> None:
    assert available_views() == ["day", "week", "month"]
