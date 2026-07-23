"""The composition root for bucketing strategies.

This is the one place that names the concrete strategies. It maps each view key
to a strategy instance so the handler can look one up by the view the request
asked for. The engine is handed the result and never imports a concrete
strategy itself.
"""

from provider_note_dashboard.aggregation.strategies import (
    DayBucketing,
    MonthBucketing,
    WeekBucketing,
)

_STRATEGIES = {
    strategy.view_key: strategy
    for strategy in (DayBucketing(), WeekBucketing(), MonthBucketing())
}


def get_strategy(view_key):
    """Return the strategy for a view key, or None when it is unknown."""
    return _STRATEGIES.get(view_key)


def available_views():
    """Return the view keys in registration order."""
    return list(_STRATEGIES.keys())
