"""The aggregation engine.

The reusable mechanism. It counts billable clinical visit notes per provider
across the buckets of a chosen view, over a bounded window. It depends only on
the bucketing contract, it is handed a strategy and calls window, buckets, and
key_for, and it never names a concrete strategy. It also depends on the shared
note selection for which notes are countable, so its counts and the detail
builder's rows are drawn from the same population. Performance is owned here,
there is no built in per provider aggregate, so the note table is queried once
over the window with the single valued relations joined, and grouped in memory.
"""

import arrow

from provider_note_dashboard.aggregation.notes import select_notes


def aggregate_counts(strategy, anchor, periods, zone):
    """Count notes per provider across the buckets of a view.

    strategy is a bucketing strategy, anchor is an arrow moment in the target
    zone, periods is how many of the view's periods to span, and zone is the
    zone name used to place each note into its calendar bucket. Returns a dict
    with the view, the window, the ordered buckets, and the provider rows sorted
    by total so the busiest provider leads.
    """
    start, end = strategy.window(anchor, periods)

    notes = select_notes(start, end).select_related("provider", "note_type_version")

    bucket_list = strategy.buckets(anchor, periods)
    bucket_keys = {bucket["key"] for bucket in bucket_list}

    providers: dict = {}
    for note in notes:
        provider = note.provider
        if provider is None:
            continue
        moment = arrow.get(note.datetime_of_service).to(zone)
        key = strategy.key_for(moment)
        if key not in bucket_keys:
            continue
        provider_id = str(provider.id)
        entry = providers.get(provider_id)
        if entry is None:
            entry = {
                "provider_id": provider_id,
                "provider_name": provider.full_name,
                "total": 0,
                "counts": {},
            }
            providers[provider_id] = entry
        entry["total"] = entry["total"] + 1
        entry["counts"][key] = entry["counts"].get(key, 0) + 1

    provider_rows = sorted(
        providers.values(), key=lambda row: row["total"], reverse=True
    )

    return {
        "view": strategy.view_key,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "buckets": bucket_list,
        "providers": provider_rows,
    }
