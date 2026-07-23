"""The shared note selection.

This is the single source of truth for which notes are countable. Both the
counting engine and the detail builder call it, so the count behind a provider
and the rows an expanded provider shows are drawn from exactly the same
population and can never drift. It owns three things, the date of service
window, the billable clinical visit note filter, and the exclusion of the
deleted and non visit note states. It owns none of the reading, each caller adds
only the joins and prefetches for the fields it reads.
"""

from canvas_sdk.v1.data.note import CurrentNoteStateEvent, Note, NoteStates

# Current note states that do not represent a written clinical visit, so they
# must not be counted. DELETED is a removed note. CANCELLED and NOSHOW are
# appointment only states for a visit that never happened. All three names are
# confirmed against the SDK NoteStates enum, where the two appointment states
# are spelled CANCELLED with the double L and NOSHOW as one word. The resolver
# below stays as a safety net so a name a future build drops still degrades to a
# skip rather than raising.
_EXCLUDED_STATE_NAMES = ("DELETED", "CANCELLED", "NOSHOW")


def _excluded_states():
    """Resolve the excluded note states, skipping any name this build cannot confirm."""
    return [
        getattr(NoteStates, name)
        for name in _EXCLUDED_STATE_NAMES
        if hasattr(NoteStates, name)
    ]


def select_notes(start, end):
    """Return the countable notes over a half open date of service window.

    start and end are arrow moments bounding the window, inclusive start and
    exclusive end. The returned queryset holds the billable clinical visit
    notes in the window with the deleted and non visit states excluded, and no
    join or prefetch, so each caller adds only the relations it reads.
    """
    excluded_note_ids = CurrentNoteStateEvent.objects.filter(
        state__in=_excluded_states()
    ).values_list("note_id", flat=True)

    return Note.objects.filter(
        datetime_of_service__gte=start.datetime,
        datetime_of_service__lt=end.datetime,
        note_type_version__is_billable=True,
    ).exclude(dbid__in=excluded_note_ids)
